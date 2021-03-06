#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#

import collections

from neutron_lib.api import validators
from neutron_lib import constants as const
from neutron_lib import exceptions as n_exc
from oslo_config import cfg
from oslo_log import log

from neutron.callbacks import events
from neutron.callbacks import registry
from neutron.callbacks import resources
from neutron import context as n_context
from neutron.db import provisioning_blocks
from neutron.extensions import multiprovidernet as mpnet
from neutron.extensions import portbindings
from neutron.extensions import portsecurity as psec
from neutron.extensions import providernet as pnet
from neutron import manager
from neutron.plugins.common import constants as plugin_const
from neutron.plugins.ml2 import driver_api
from neutron.services.qos import qos_consts

from networking_ovn._i18n import _, _LI, _LW
from networking_ovn.common import acl as ovn_acl
from networking_ovn.common import config
from networking_ovn.common import constants as ovn_const
from networking_ovn.common import utils
from networking_ovn.ml2 import qos_driver
from networking_ovn import ovn_db_sync
from networking_ovn.ovsdb import impl_idl_ovn
from networking_ovn.ovsdb import ovsdb_monitor


LOG = log.getLogger(__name__)

OvnPortInfo = collections.namedtuple('OvnPortInfo', ['type', 'options',
                                                     'addresses',
                                                     'port_security',
                                                     'parent_name', 'tag'])


class OVNMechanismDriver(driver_api.MechanismDriver):
    """OVN ML2 mechanism driver

    A mechanism driver is called on the creation, update, and deletion
    of networks and ports. For every event, there are two methods that
    get called - one within the database transaction (method suffix of
    _precommit), one right afterwards (method suffix of _postcommit).

    Exceptions raised by methods called inside the transaction can
    rollback, but should not make any blocking calls (for example,
    REST requests to an outside controller). Methods called after
    transaction commits can make blocking external calls, though these
    will block the entire process. Exceptions raised in calls after
    the transaction commits may cause the associated resource to be
    deleted.

    Because rollback outside of the transaction is not done in the
    update network/port case, all data validation must be done within
    methods that are part of the database transaction.
    """

    supported_qos_rule_types = [qos_consts.RULE_TYPE_BANDWIDTH_LIMIT]

    def initialize(self):
        """Perform driver initialization.

        Called after all drivers have been loaded and the database has
        been initialized. No abstract methods defined below will be
        called prior to this method being called.
        """
        LOG.info(_LI("Starting OVNMechanismDriver"))
        self._nb_ovn = None
        self._sb_ovn = None
        self._plugin_property = None
        self.sg_enabled = ovn_acl.is_sg_enabled()
        if cfg.CONF.SECURITYGROUP.firewall_driver:
            LOG.warning(_LW('Firewall driver configuration is ignored'))
        self._setup_vif_port_bindings()
        self.subscribe()
        self.qos_driver = qos_driver.OVNQosDriver(self)

    @property
    def _plugin(self):
        if self._plugin_property is None:
            self._plugin_property = manager.NeutronManager.get_plugin()
        return self._plugin_property

    def _get_attribute(self, obj, attribute):
        res = obj.get(attribute)
        if res is const.ATTR_NOT_SPECIFIED:
            res = None
        return res

    def _setup_vif_port_bindings(self):
        self.supported_vnic_types = [portbindings.VNIC_NORMAL]
        # NOTE(rtheis): Config for vif_type will ensure valid choices.
        if config.get_ovn_vif_type() == portbindings.VIF_TYPE_VHOST_USER:
            self.vif_type = portbindings.VIF_TYPE_VHOST_USER
            self.vif_details = {
                portbindings.CAP_PORT_FILTER: False,
                portbindings.VHOST_USER_MODE:
                portbindings.VHOST_USER_MODE_CLIENT,
                portbindings.VHOST_USER_OVS_PLUG: True,
            }
        else:
            self.vif_type = portbindings.VIF_TYPE_OVS,
            self.vif_details = {
                portbindings.CAP_PORT_FILTER: self.sg_enabled,
            }

    def subscribe(self):
        registry.subscribe(self.post_fork_initialize,
                           resources.PROCESS,
                           events.AFTER_CREATE)

        # Handle security group/rule notifications
        if self.sg_enabled:
            registry.subscribe(self._process_sg_notification,
                               resources.SECURITY_GROUP,
                               events.AFTER_UPDATE)
            registry.subscribe(self._process_sg_notification,
                               resources.SECURITY_GROUP_RULE,
                               events.AFTER_CREATE)
            registry.subscribe(self._process_sg_notification,
                               resources.SECURITY_GROUP_RULE,
                               events.BEFORE_DELETE)

    def post_fork_initialize(self, resource, event, trigger, **kwargs):
        # NOTE(rtheis): This will initialize all workers (API, RPC,
        # plugin service and OVN) with OVN IDL connections.
        self._nb_ovn, self._sb_ovn = impl_idl_ovn.get_ovn_idls(self,
                                                               trigger)

        if trigger.im_class == ovsdb_monitor.OvnWorker:
            # Call the synchronization task if its ovn worker
            # This sync neutron DB to OVN-NB DB only in inconsistent states
            self.nb_synchronizer = ovn_db_sync.OvnNbSynchronizer(
                self._plugin,
                self._nb_ovn,
                config.get_ovn_neutron_sync_mode(),
                self
            )
            self.nb_synchronizer.sync()

    def _process_sg_notification(self, resource, event, trigger, **kwargs):
        sg_id = None
        sg_rule = None
        is_add_acl = True

        admin_context = n_context.get_admin_context()
        if resource == resources.SECURITY_GROUP:
            sg_id = kwargs.get('security_group_id')
        elif resource == resources.SECURITY_GROUP_RULE:
            if event == events.AFTER_CREATE:
                sg_rule = kwargs.get('security_group_rule')
                sg_id = sg_rule['security_group_id']
            elif event == events.BEFORE_DELETE:
                sg_rule = self._plugin.get_security_group_rule(
                    admin_context, kwargs.get('security_group_rule_id'))
                sg_id = sg_rule['security_group_id']
                is_add_acl = False

        # TODO(russellb) It's possible for Neutron and OVN to get out of sync
        # here. If updating ACls fails somehow, we're out of sync until another
        # change causes another refresh attempt.
        ovn_acl.update_acls_for_security_group(self._plugin,
                                               admin_context,
                                               self._nb_ovn,
                                               sg_id,
                                               rule=sg_rule,
                                               is_add_acl=is_add_acl)

    def create_network_precommit(self, context):
        """Allocate resources for a new network.

        :param context: NetworkContext instance describing the new
        network.

        Create a new network, allocating resources as necessary in the
        database. Called inside transaction context on session. Call
        cannot block.  Raising an exception will result in a rollback
        of the current transaction.
        """
        network = context.current

        # TODO(rtheis): Add support for multi-provider networks when
        # routed networks are supported.
        if self._get_attribute(network, mpnet.SEGMENTS):
            msg = _('Multi-provider networks are not supported')
            raise n_exc.InvalidInput(error_message=msg)

        network_segments = context.network_segments
        network_type = network_segments[0]['network_type']
        segmentation_id = network_segments[0]['segmentation_id']
        physical_network = network_segments[0]['physical_network']
        LOG.debug('Creating network with type %(network_type)s, '
                  'segmentation ID %(segmentation_id)s, '
                  'physical network %(physical_network)s' %
                  {'network_type': network_type,
                   'segmentation_id': segmentation_id,
                   'physical_network': physical_network})

        if network_type not in [plugin_const.TYPE_LOCAL,
                                plugin_const.TYPE_FLAT,
                                plugin_const.TYPE_GENEVE,
                                plugin_const.TYPE_VLAN]:
            msg = _('Network type %s is not supported') % network_type
            raise n_exc.InvalidInput(error_message=msg)

    def create_network_postcommit(self, context):
        """Create a network.

        :param context: NetworkContext instance describing the new
        network.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        cause the deletion of the resource.
        """
        network = context.current
        physnet = self._get_attribute(network, pnet.PHYSICAL_NETWORK)
        segid = self._get_attribute(network, pnet.SEGMENTATION_ID)
        self.create_network_in_ovn(network, {}, physnet, segid)

    def create_network_in_ovn(self, network, ext_ids,
                              physnet=None, segid=None):
        # Create a logical switch with a name equal to the Neutron network
        # UUID.  This provides an easy way to refer to the logical switch
        # without having to track what UUID OVN assigned to it.
        ext_ids.update({
            ovn_const.OVN_NETWORK_NAME_EXT_ID_KEY: network['name']
        })

        lswitch_name = utils.ovn_name(network['id'])
        with self._nb_ovn.transaction(check_error=True) as txn:
            txn.add(self._nb_ovn.create_lswitch(
                lswitch_name=lswitch_name,
                external_ids=ext_ids))
            if physnet:
                vlan_id = None
                if segid is not None:
                    vlan_id = int(segid)
                txn.add(self._nb_ovn.create_lswitch_port(
                    lport_name='provnet-%s' % network['id'],
                    lswitch_name=lswitch_name,
                    addresses=['unknown'],
                    external_ids=None,
                    type='localnet',
                    tag=vlan_id,
                    options={'network_name': physnet}))
        return network

    def _set_network_name(self, network_id, name):
        ext_id = [ovn_const.OVN_NETWORK_NAME_EXT_ID_KEY, name]
        self._nb_ovn.set_lswitch_ext_id(
            utils.ovn_name(network_id), ext_id).execute(check_error=True)

    def update_network_postcommit(self, context):
        """Update a network.

        :param context: NetworkContext instance describing the new
        state of the network, as well as the original state prior
        to the update_network call.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        cause the deletion of the resource.

        update_network_postcommit is called for all changes to the
        network state.  It is up to the mechanism driver to ignore
        state or state changes that it does not know or care about.
        """
        network = context.current
        original_network = context.original
        if network['name'] != original_network['name']:
            self._set_network_name(network['id'], network['name'])
        self.qos_driver.update_network(network, original_network)

    def delete_network_postcommit(self, context):
        """Delete a network.

        :param context: NetworkContext instance describing the current
        state of the network, prior to the call to delete it.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Runtime errors are not
        expected, and will not prevent the resource from being
        deleted.
        """
        network = context.current
        self._nb_ovn.delete_lswitch(
            utils.ovn_name(network['id']), if_exists=True).execute(
                check_error=True)

    def create_port_precommit(self, context):
        """Allocate resources for a new port.

        :param context: PortContext instance describing the port.

        Create a new port, allocating resources as necessary in the
        database. Called inside transaction context on session. Call
        cannot block.  Raising an exception will result in a rollback
        of the current transaction.
        """
        self.validate_and_get_data_from_binding_profile(context.current)

    # TODO(rtheis): The neutron ML2 plugin does not provide drivers with
    # an interface to validate port bindings before the precommit stage.
    # As a result, the plugin will map a validate error here to a
    # MechanismDriverError. This makes it harder for users to determine
    # mistakes in the binding profile. Refactor this as needed based
    # on resolution to https://bugs.launchpad.net/neutron/+bug/1589622.
    def validate_and_get_data_from_binding_profile(self, port):
        if (ovn_const.OVN_PORT_BINDING_PROFILE not in port or
                not validators.is_attr_set(
                    port[ovn_const.OVN_PORT_BINDING_PROFILE])):
            return {}
        param_set = {}
        param_dict = {}
        for param_set in ovn_const.OVN_PORT_BINDING_PROFILE_PARAMS:
            param_keys = param_set.keys()
            for param_key in param_keys:
                try:
                    param_dict[param_key] = (port[
                        ovn_const.OVN_PORT_BINDING_PROFILE][param_key])
                except KeyError:
                    pass
            if len(param_dict) == 0:
                continue
            if len(param_dict) != len(param_keys):
                msg = _('Invalid binding:profile. %s are all '
                        'required.') % param_keys
                raise n_exc.InvalidInput(error_message=msg)
            if (len(port[ovn_const.OVN_PORT_BINDING_PROFILE]) != len(
                    param_keys)):
                msg = _('Invalid binding:profile. too many parameters')
                raise n_exc.InvalidInput(error_message=msg)
            break

        if not param_dict:
            return {}

        for param_key, param_type in param_set.items():
            if param_type is None:
                continue
            param_value = param_dict[param_key]
            if not isinstance(param_value, param_type):
                msg = _('Invalid binding:profile. %(key)s %(value)s '
                        'value invalid type') % {'key': param_key,
                                                 'value': param_value}
                raise n_exc.InvalidInput(error_message=msg)

        # Make sure we can successfully look up the port indicated by
        # parent_name.  Just let it raise the right exception if there is a
        # problem.
        if 'parent_name' in param_set:
            self._plugin.get_port(n_context.get_admin_context(),
                                  param_dict['parent_name'])

        if 'tag' in param_set:
            tag = int(param_dict['tag'])
            if tag < 0 or tag > 4095:
                msg = _('Invalid binding:profile. tag "%s" must be '
                        'an integer between 0 and 4095, inclusive') % tag
                raise n_exc.InvalidInput(error_message=msg)

        return param_dict

    def _insert_port_provisioning_block(self, port):
        vnic_type = port.get(portbindings.VNIC_TYPE, portbindings.VNIC_NORMAL)
        if vnic_type not in self.supported_vnic_types:
            LOG.debug("No provisioning block due to unsupported vnic_type: %s",
                      vnic_type)
            return
        # Insert a provisioning block to prevent the port from
        # transitioning to active until OVN reports back that
        # the port is up.
        if port['status'] != const.PORT_STATUS_ACTIVE:
            provisioning_blocks.add_provisioning_component(
                n_context.get_admin_context(),
                port['id'], resources.PORT,
                provisioning_blocks.L2_AGENT_ENTITY
            )

    def create_port_postcommit(self, context):
        """Create a port.

        :param context: PortContext instance describing the port.

        Called after the transaction completes. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance.  Raising an exception will
        result in the deletion of the resource.
        """
        port = context.current
        ovn_port_info = self.get_ovn_port_options(port)
        self._insert_port_provisioning_block(port)
        self.create_port_in_ovn(port, ovn_port_info)

    def _get_allowed_addresses_from_port(self, port):
        if not port.get(psec.PORTSECURITY):
            return []

        allowed_addresses = set()
        addresses = port['mac_address']
        for ip in port.get('fixed_ips', []):
            addresses += ' ' + ip['ip_address']

        for allowed_address in port.get('allowed_address_pairs', []):
            # If allowed address pair has same mac as the port mac,
            # append the allowed ip address to the 'addresses'.
            # Else we will have multiple entries for the same mac in
            # 'Logical_Switch_Port.port_security'.
            if allowed_address['mac_address'] == port['mac_address']:
                addresses += ' ' + allowed_address['ip_address']
            else:
                allowed_addresses.add(allowed_address['mac_address'] + ' ' +
                                      allowed_address['ip_address'])

        allowed_addresses.add(addresses)

        return list(allowed_addresses)

    def get_ovn_port_options(self, port, qos_options=None):
        binding_profile = self.validate_and_get_data_from_binding_profile(port)
        if qos_options is None:
            qos_options = self.qos_driver.get_qos_options(port)
        vtep_physical_switch = binding_profile.get('vtep_physical_switch')
        parent_name = None
        tag = None
        port_type = None

        if vtep_physical_switch:
            vtep_logical_switch = binding_profile.get('vtep_logical_switch')
            port_type = 'vtep'
            options = {'vtep_physical_switch': vtep_physical_switch,
                       'vtep_logical_switch': vtep_logical_switch}
            addresses = "unknown"
            port_security = []
        else:
            options = qos_options
            parent_name = binding_profile.get('parent_name')
            tag = binding_profile.get('tag')
            addresses = port['mac_address']
            for ip in port.get('fixed_ips', []):
                addresses += ' ' + ip['ip_address']
            port_security = self._get_allowed_addresses_from_port(port)

        return OvnPortInfo(port_type, options, [addresses], port_security,
                           parent_name, tag)

    def create_port_in_ovn(self, port, ovn_port_info):
        external_ids = {ovn_const.OVN_PORT_NAME_EXT_ID_KEY: port['name']}
        lswitch_name = utils.ovn_name(port['network_id'])
        admin_context = n_context.get_admin_context()
        sg_cache = {}
        sg_ports_cache = {}
        subnet_cache = {}

        with self._nb_ovn.transaction(check_error=True) as txn:
            # The lport_name *must* be neutron port['id'].  It must match the
            # iface-id set in the Interfaces table of the Open_vSwitch
            # database which nova sets to be the port ID.
            txn.add(self._nb_ovn.create_lswitch_port(
                    lport_name=port['id'],
                    lswitch_name=lswitch_name,
                    addresses=ovn_port_info.addresses,
                    external_ids=external_ids,
                    parent_name=ovn_port_info.parent_name,
                    tag=ovn_port_info.tag,
                    enabled=port.get('admin_state_up'),
                    options=ovn_port_info.options,
                    type=ovn_port_info.type,
                    port_security=ovn_port_info.port_security))
            acls_new = ovn_acl.add_acls(self._plugin, admin_context,
                                        port, sg_cache, sg_ports_cache,
                                        subnet_cache)
            for acl in acls_new:
                txn.add(self._nb_ovn.add_acl(**acl))

        if len(port.get('fixed_ips')):
            for sg_id in port.get('security_groups', []):
                ovn_acl.refresh_remote_security_group(
                    self._plugin, admin_context, self._nb_ovn,
                    sg_id, sg_cache, sg_ports_cache,
                    subnet_cache, [port['id']])

    def update_port_precommit(self, context):
        """Update resources of a port.

        :param context: PortContext instance describing the new
        state of the port, as well as the original state prior
        to the update_port call.

        Called inside transaction context on session to complete a
        port update as defined by this mechanism driver. Raising an
        exception will result in rollback of the transaction.

        update_port_precommit is called for all changes to the port
        state. It is up to the mechanism driver to ignore state or
        state changes that it does not know or care about.
        """
        self.validate_and_get_data_from_binding_profile(context.current)

    def update_port_postcommit(self, context):
        """Update a port.

        :param context: PortContext instance describing the new
        state of the port, as well as the original state prior
        to the update_port call.

        Called after the transaction completes. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance.  Raising an exception will
        result in the deletion of the resource.

        update_port_postcommit is called for all changes to the port
        state. It is up to the mechanism driver to ignore state or
        state changes that it does not know or care about.
        """
        port = context.current
        original_port = context.original
        self.update_port(port, original_port)

    def update_port(self, port, original_port, qos_options=None):
        ovn_port_info = self.get_ovn_port_options(port, qos_options)
        self._update_port_in_ovn(original_port, port, ovn_port_info)

    def _update_port_in_ovn(self, original_port, port, ovn_port_info):
        external_ids = {
            ovn_const.OVN_PORT_NAME_EXT_ID_KEY: port['name']}
        admin_context = n_context.get_admin_context()
        sg_cache = {}
        sg_ports_cache = {}
        subnet_cache = {}

        with self._nb_ovn.transaction(check_error=True) as txn:
            txn.add(self._nb_ovn.set_lswitch_port(
                    lport_name=port['id'],
                    addresses=ovn_port_info.addresses,
                    external_ids=external_ids,
                    parent_name=ovn_port_info.parent_name,
                    tag=ovn_port_info.tag,
                    type=ovn_port_info.type,
                    options=ovn_port_info.options,
                    enabled=port['admin_state_up'],
                    port_security=ovn_port_info.port_security))
            # Note that the ovsdb IDL suppresses the transaction down to what
            # has actually changed.
            txn.add(self._nb_ovn.delete_acl(
                    utils.ovn_name(port['network_id']),
                    port['id']))
            acls_new = ovn_acl.add_acls(self._plugin,
                                        admin_context,
                                        port,
                                        sg_cache,
                                        sg_ports_cache,
                                        subnet_cache)
            for acl in acls_new:
                txn.add(self._nb_ovn.add_acl(**acl))

        # Refresh remote security groups for changed security groups
        old_sg_ids = set(original_port.get('security_groups', []))
        new_sg_ids = set(port.get('security_groups', []))
        detached_sg_ids = old_sg_ids - new_sg_ids
        attached_sg_ids = new_sg_ids - old_sg_ids

        if (len(port.get('fixed_ips')) == 0 and
                len(original_port.get('fixed_ips')) == 0):
            # No need to process remote security group if the port
            # didn't have any IP Addresses.
            return port

        for sg_id in (attached_sg_ids | detached_sg_ids):
            ovn_acl.refresh_remote_security_group(
                self._plugin, admin_context, self._nb_ovn, sg_id,
                sg_cache, sg_ports_cache,
                subnet_cache, [port['id']])

        # Refresh remote security groups if remote_group_match_ip is set
        if original_port.get('fixed_ips') != port.get('fixed_ips'):
            # We have refreshed attached and detached security groups, so
            # now we only need to take care of unchanged security groups.
            unchanged_sg_ids = new_sg_ids & old_sg_ids
            for sg_id in unchanged_sg_ids:
                ovn_acl.refresh_remote_security_group(
                    self._plugin, admin_context, self._nb_ovn, sg_id,
                    sg_cache, sg_ports_cache,
                    subnet_cache, [port['id']])

    def delete_port_postcommit(self, context):
        """Delete a port.

        :param context: PortContext instance describing the current
        state of the port, prior to the call to delete it.

        Called after the transaction completes. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance.  Runtime errors are not
        expected, and will not prevent the resource from being
        deleted.
        """
        port = context.current
        with self._nb_ovn.transaction(check_error=True) as txn:
            txn.add(self._nb_ovn.delete_lswitch_port(port['id'],
                    utils.ovn_name(port['network_id'])))
            txn.add(self._nb_ovn.delete_acl(
                    utils.ovn_name(port['network_id']), port['id']))

        admin_context = n_context.get_admin_context()
        sg_ids = port.get('security_groups', [])
        num_fixed_ips = len(port.get('fixed_ips'))
        if num_fixed_ips:
            for sg_id in sg_ids:
                ovn_acl.refresh_remote_security_group(
                    self._plugin, admin_context, self._nb_ovn, sg_id)

    def bind_port(self, context):
        """Attempt to bind a port.

        :param context: PortContext instance describing the port

        This method is called outside any transaction to attempt to
        establish a port binding using this mechanism driver. Bindings
        may be created at each of multiple levels of a hierarchical
        network, and are established from the top level downward. At
        each level, the mechanism driver determines whether it can
        bind to any of the network segments in the
        context.segments_to_bind property, based on the value of the
        context.host property, any relevant port or network
        attributes, and its own knowledge of the network topology. At
        the top level, context.segments_to_bind contains the static
        segments of the port's network. At each lower level of
        binding, it contains static or dynamic segments supplied by
        the driver that bound at the level above. If the driver is
        able to complete the binding of the port to any segment in
        context.segments_to_bind, it must call context.set_binding
        with the binding details. If it can partially bind the port,
        it must call context.continue_binding with the network
        segments to be used to bind at the next lower level.

        If the binding results are committed after bind_port returns,
        they will be seen by all mechanism drivers as
        update_port_precommit and update_port_postcommit calls. But if
        some other thread or process concurrently binds or updates the
        port, these binding results will not be committed, and
        update_port_precommit and update_port_postcommit will not be
        called on the mechanism drivers with these results. Because
        binding results can be discarded rather than committed,
        drivers should avoid making persistent state changes in
        bind_port, or else must ensure that such state changes are
        eventually cleaned up.

        Implementing this method explicitly declares the mechanism
        driver as having the intention to bind ports. This is inspected
        by the QoS service to identify the available QoS rules you
        can use with ports.
        """
        port = context.current
        vnic_type = port.get(portbindings.VNIC_TYPE, portbindings.VNIC_NORMAL)
        if vnic_type not in self.supported_vnic_types:
            LOG.debug("Refusing to bind due to unsupported vnic_type: %s",
                      vnic_type)
            return
        for segment_to_bind in context.segments_to_bind:
            if self.vif_type == portbindings.VIF_TYPE_VHOST_USER:
                port[portbindings.VIF_DETAILS].update({
                    portbindings.VHOST_USER_SOCKET: utils.ovn_vhu_sockpath(
                        cfg.CONF.ovn.vhost_sock_dir, port['id'])
                    })
            context.set_binding(segment_to_bind[driver_api.ID],
                                self.vif_type,
                                self.vif_details)

    def get_workers(self):
        """Get any NeutronWorker instances that should have their own process

        Any driver that needs to run processes separate from the API or RPC
        workers, can return a sequence of NeutronWorker instances.
        """
        # See doc/source/design/ovn_worker.rst for more details.
        return [ovsdb_monitor.OvnWorker()]

    def set_port_status_up(self, port_id):
        # Port provisioning is complete now that OVN has reported
        # that the port is up.
        LOG.debug("OVN reports status up for port: %s", port_id)
        provisioning_blocks.provisioning_complete(
            n_context.get_admin_context(),
            port_id,
            resources.PORT,
            provisioning_blocks.L2_AGENT_ENTITY)

    def set_port_status_down(self, port_id):
        LOG.debug("OVN reports status down for port: %s", port_id)
        self._plugin.update_port_status(n_context.get_admin_context(),
                                        port_id,
                                        const.PORT_STATUS_DOWN)
