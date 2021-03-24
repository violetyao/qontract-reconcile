import logging
import os

import requests
import jinja2

from reconcile import queries
from reconcile.utils import threaded
from reconcile.utils.secret_reader import SecretReader


LOG = logging.getLogger(__name__)
# QONTRACT_INTEGRATION = 'dashdotdb-slo'
QONTRACT_INTEGRATION = 'dashdotdb-cso'


DASHDOTDB_SECRET = os.environ.get('DASHDOTDB_SECRET',
                                  'app-sre/dashdot/auth-proxy-production')


class DashdotdbSLO:
    def __init__(self, dry_run, thread_pool_size):
        self.dry_run = dry_run
        self.thread_pool_size = thread_pool_size
        self.settings = queries.get_app_interface_settings()
        secret_reader = SecretReader(settings=self.settings)

        secret_content = secret_reader.read_all({'path': DASHDOTDB_SECRET})

        # self.dashdotdb_url = secret_content['url']
        # self.dashdotdb_user = secret_content['username']
        # self.dashdotdb_pass = secret_content['password']
        self.dashdotdb_url = "http://localhost:8080/"
        self.dashdotdb_user = "postgres"
        self.dashdotdb_pass = "postgres"

    def _post(self, service_slo):
        if service_slo is None:
            return None

        response = None

        if self.dry_run:
            return response

        for item in service_slo:
            LOG.info('SLO: syncing slo %s', item['name'])
            cluster = item['cluster']['name']
            endpoint = (f'{self.dashdotdb_url}/api/v1/'
                        f'serviceslometrics/{cluster}')
            response = requests.post(url=endpoint, 
                                     json=item,
                                     auth=(self.dashdotdb_user,
                                           self.dashdotdb_pass))
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as details:
                LOG.error('SLO: error posting %s - %s',
                          item['name'],
                          details)

            LOG.info('SLO: slo %s synced', item['name'])
        return response
    
    def _get_automationtoken(self, tokenpath):
        autotoken_reader = SecretReader(settings=self.settings)
        token = autotoken_reader.read(tokenpath)
        return token

    def _promget(self, url, query, token=None, ssl_verify=True):
        uri = '/api/v1/query'
        url = urljoin((f'{url}'), uri)
        params = {'query': (f'{query}')}
        LOG.debug('%s Fetching prom payload from %s?%s',
                  self.logmarker, url, params)
        headers = {
                   "accept": "application/json",
                  }
        if token:
            headers["Authorization"] = (f"Bearer {token}")
        response = requests.get(url,
                                params=params,
                                headers=headers,
                                verify=ssl_verify,
                                timeout=(5, 120))
        response.raise_for_status()

        response = response.json()
        # TODO ensure len response == 1
        # return response['data']['result']
        print(response)
        return response

    def _get_service_slo(self, slo_document):
        LOG.info('SLO: processing %s', slo_document['name'])
        result = []
        for ns in slo_document['namespaces']:
            if not ns['cluster'].get('prometheusUrl'):
                # promurl = f"https://prometheus.{ns['cluster']['name']}.devshift.net/"
                continue
            promurl = ns['cluster']['prometheusUrl']
            ssl_verify = False if ns['cluster']['spec']['private'] else True
            # promtoken = self._get_automationtoken(
            #                 ns['cluster']['automationToken'])
            for slo in slo_document['slos']:
                unit = slo['SLOTargetUnit']
                expr = slo['expr']
                template = jinja2.Template(expr)
                window = slo['SLOParameters']['window']
                promquery = template.render({"window": window})
                # value = self._promget(url=promurl, 
                #                       query=promquery,
                #                       token=promtoken,
                #                       ssl_verify=ssl_verify)
                result.append({
                    "name": slo['name'],
                    "SLIType": slo['SLIType'],
                    "namespace": ns,
                    "cluster": ns['cluster'],
                    "service": ns['app'],
                    # "value": value,
                    "value": 50,
                    "target": slo['SLOTarget'],
                })
        return result

    def run(self):
        clusters = queries.get_clusters()
        slo_documents = queries.get_slo_documents()
        # print(slo_documents)
        service_slos = threaded.run(func=self._get_service_slo,
                                    iterable=slo_documents,
                                    thread_pool_size=self.thread_pool_size)
        # print(service_slos)
        threaded.run(func=self._post,
                     iterable=service_slos,
                     thread_pool_size=self.thread_pool_size)


def run(dry_run=False, thread_pool_size=10):
    dashdotdb_slo = DashdotdbSLO(dry_run, thread_pool_size)
    dashdotdb_slo.run()
