import logging
import sys
import json


import reconcile.queries as queries

from reconcile.utils.aws_api import AWSApi
from reconcile.utils.defer import defer
from reconcile.utils.ocm import OCMMap
from reconcile.utils.terraform_client import TerraformClient as Terraform
from reconcile.utils.terrascript_client import TerrascriptClient as Terrascript
from reconcile.utils.semver_helper import make_semver


QONTRACT_INTEGRATION = 'terraform_tgw_attachments'
QONTRACT_INTEGRATION_VERSION = make_semver(0, 1, 0)


def build_desired_state_tgw_attachments(clusters, ocm_map, settings):
    """
    Fetch state for TGW attachments between a cluster and all TGWs
    in an account in the same region as the cluster
    """
    desired_state = []
    error = False

    for cluster_info in clusters:
        cluster = cluster_info['name']
        ocm = ocm_map.get(cluster)
        peering_info = cluster_info['peering']
        peer_connections = peering_info['connections']
        for peer_connection in peer_connections:
            # We only care about account-tgw peering providers
            peer_connection_provider = peer_connection['provider']
            if not peer_connection_provider == 'account-tgw':
                continue
            # accepter is the cluster's AWS account
            cluster_region = cluster_info['spec']['region']
            accepter = {
                'cidr_block': cluster_info['network']['vpc'],
                'region': cluster_region
            }

            account = peer_connection['account']
            # assume_role is the role to assume to provision the
            # peering connection request, through the accepter AWS account.
            account['assume_role'] = \
                ocm.get_aws_infrastructure_access_terraform_assume_role(
                    cluster,
                    account['uid'],
                    account['terraformUsername']
                )
            account['assume_region'] = accepter['region']
            account['assume_cidr'] = accepter['cidr_block']
            aws_api = AWSApi(1, [account], settings=settings)
            accepter_vpc_id, accepter_route_table_ids, \
                accepter_subnets_id_az = \
                aws_api.get_cluster_vpc_details(
                    account,
                    route_tables=peer_connection.get('manageRoutes'),
                    subnets=True,
                )

            if accepter_vpc_id is None:
                logging.error(f'[{cluster} could not find VPC ID for cluster')
                error = True
                continue
            accepter['vpc_id'] = accepter_vpc_id
            accepter['route_table_ids'] = accepter_route_table_ids
            accepter['subnets_id_az'] = accepter_subnets_id_az
            accepter['account'] = account

            account_tgws = \
                aws_api.get_tgws_details(
                    account,
                    cluster_region,
                    tags=json.loads(peer_connection.get('tags') or {}),
                    route_tables=peer_connection.get('manageRoutes'),
                )
            for tgw in account_tgws:
                tgw_id = tgw['tgw_id']
                connection_name = \
                    f"{peer_connection['name']}_" + \
                    f"{account['name']}-{tgw_id}"
                requester = {
                    'tgw_id': tgw_id,
                    'tgw_arn': tgw['tgw_arn'],
                    'region': tgw['region'],
                    'cidr_block': peer_connection.get('cidrBlock'),
                    'account': account,
                }
                item = {
                    'connection_provider': peer_connection_provider,
                    'connection_name': connection_name,
                    'requester': requester,
                    'accepter': accepter,
                    'deleted': peer_connection.get('delete', False)
                }
                desired_state.append(item)

    return desired_state, error


@defer
def run(dry_run, print_only=False,
        enable_deletion=False, thread_pool_size=10, defer=None):
    settings = queries.get_app_interface_settings()
    clusters = [c for c in queries.get_clusters()
                if c.get('peering') is not None]
    ocm_map = OCMMap(clusters=clusters, integration=QONTRACT_INTEGRATION,
                     settings=settings)

    # Fetch desired state for cluster-to-vpc(account) VPCs
    desired_state, err = \
        build_desired_state_tgw_attachments(clusters, ocm_map, settings)
    if err:
        sys.exit(1)

    # check there are no repeated vpc connection names
    connection_names = [c['connection_name'] for c in desired_state]
    if len(set(connection_names)) != len(connection_names):
        logging.error("duplicate vpc connection names found")
        sys.exit(1)

    participating_accounts = \
        [item['requester']['account'] for item in desired_state]
    participating_accounts += \
        [item['accepter']['account'] for item in desired_state]
    participating_account_names = \
        [a['name'] for a in participating_accounts]
    accounts = [a for a in queries.get_aws_accounts()
                if a['name'] in participating_account_names]

    ts = Terrascript(QONTRACT_INTEGRATION,
                     "",
                     thread_pool_size,
                     accounts,
                     settings=settings)
    ts.populate_additional_providers(participating_accounts)
    ts.populate_tgw_attachments(desired_state)
    working_dirs = ts.dump(print_only=print_only)

    if print_only:
        sys.exit()

    tf = Terraform(QONTRACT_INTEGRATION,
                   QONTRACT_INTEGRATION_VERSION,
                   "",
                   accounts,
                   working_dirs,
                   thread_pool_size)

    if tf is None:
        sys.exit(1)

    defer(lambda: tf.cleanup())

    disabled_deletions_detected, err = tf.plan(enable_deletion)
    if err:
        sys.exit(1)
    if disabled_deletions_detected:
        sys.exit(1)

    if dry_run:
        return

    err = tf.apply()
    if err:
        sys.exit(1)
