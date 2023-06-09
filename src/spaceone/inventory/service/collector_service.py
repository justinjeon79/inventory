import logging
import json
from jsonschema import validate
from spaceone.core.service import *
from spaceone.core import utils
from spaceone.core import queue
from spaceone.core.scheduler.task_schema import SPACEONE_TASK_SCHEMA
from spaceone.inventory.error import *
from spaceone.inventory.manager.collection_state_manager import CollectionStateManager
from spaceone.inventory.manager.collector_manager import CollectorManager
from spaceone.inventory.manager.collecting_manager import CollectingManager
from spaceone.inventory.manager.collector_plugin_manager import CollectorPluginManager
from spaceone.inventory.manager.collector_rule_manager import CollectorRuleManager
from spaceone.inventory.manager.repository_manager import RepositoryManager
from spaceone.inventory.manager.plugin_manager import PluginManager
from spaceone.inventory.manager.secret_manager import SecretManager
from spaceone.inventory.manager.job_manager import JobManager
from spaceone.inventory.manager.job_task_manager import JobTaskManager

_LOGGER = logging.getLogger(__name__)
_KEYWORD_FILTER = ['collector_id', 'name', 'provider']


@authentication_handler
@authorization_handler
@mutation_handler
@event_handler
class CollectorService(BaseService):

    def __init__(self, metadata):
        super().__init__(metadata)

    @transaction(append_meta={'authorization.scope': 'DOMAIN'})
    @check_required(['name', 'plugin_info', 'domain_id'])
    def create(self, params):
        """
        Args:
            params (dict): {
                'name': 'str',
                'plugin_info': 'dict',
                'provider': 'str',
                'schedule': 'dict',
                'tags': 'dict',
                'domain_id': 'str'
            }

        Returns:
            collector_vo (object)
        """
        collector_mgr: CollectorManager = self.locator.get_manager(CollectorManager)
        collector_plugin_mgr: CollectorPluginManager = self.locator.get_manager(CollectorPluginManager)
        plugin_manager = self.locator.get_manager(PluginManager)

        plugin_info = params['plugin_info']
        domain_id = params['domain_id']

        if 'tags' in params:
            params['tags'] = self._convert_tags(params.get('tags'))

        plugin_info_from_repository = self._get_plugin_from_repository(params['plugin_info'], params['domain_id'])

        params.update({
            'capability': plugin_info_from_repository.get('capability', {}),
            'provider': self._get_plugin_providers(params.get('provider'), plugin_info_from_repository)
        })

        if 'schedule' in params:
            collector_mgr.is_supported_schedule(params['plugin_info'], params['schedule'])

        collector_vo = collector_mgr.create_collector(params)

        endpoint, updated_version = plugin_manager.get_endpoint(plugin_info['plugin_id'],
                                                                plugin_info.get('version'),
                                                                domain_id,
                                                                plugin_info.get('upgrade_mode', 'AUTO'))

        plugin_response = collector_plugin_mgr.init_plugin(endpoint, plugin_info.get('options', {}))

        if updated_version:
            plugin_info['version'] = updated_version

        plugin_info['metadata'] = plugin_response.get('metadata', {})
        plugin_info_params = {'plugin_info': plugin_info}

        collector_vo = collector_mgr.update_collector_by_vo(collector_vo, plugin_info_params)
        self.create_collector_rules_by_metadata(plugin_info['metadata'], collector_vo.collector_id, params['domain_id'])

        return collector_vo

    @transaction(append_meta={'authorization.scope': 'DOMAIN'})
    @check_required(['collector_id', 'domain_id'])
    def update(self, params):
        """
        Args:
            params (dict): {
                'collector_id': 'str',
                'name': 'str',
                'priority': 'int',
                'tags': 'dict',
                'schedule': 'dict',
                'domain_id': 'str'
            }

        Returns:
            collector_vo (object)
        """
        collector_mgr: CollectorManager = self.locator.get_manager(CollectorManager)
        collector_vo = collector_mgr.get_collector(params['collector_id'], params['domain_id'])

        if 'tags' in params:
            params['tags'] = self._convert_tags(params.get('tags'))

        if 'schedule' in params:
            collector_dict = collector_vo.to_dict()
            collector_mgr.is_supported_schedule(collector_dict.get('plugin_info', {}), params['schedule'])

        return collector_mgr.update_collector_by_vo(collector_vo, params)

    @transaction(append_meta={'authorization.scope': 'DOMAIN'})
    @check_required(['collector_id', 'domain_id'])
    def delete(self, params):
        collector_mgr: CollectorManager = self.locator.get_manager(CollectorManager)
        state_mgr: CollectionStateManager = self.locator.get_manager(CollectionStateManager)

        collector_mgr.delete_collector(params['collector_id'], params['domain_id'])
        state_mgr.delete_collection_state_by_collector_id(params['collector_id'], params['domain_id'])

    @transaction(append_meta={'authorization.scope': 'DOMAIN'})
    @check_required(['collector_id', 'domain_id'])
    def get(self, params):
        collector_mgr: CollectorManager = self.locator.get_manager(CollectorManager)
        collector_id = params['collector_id']
        domain_id = params['domain_id']
        only = params.get('only')
        return collector_mgr.get_collector(collector_id, domain_id, only)

    @transaction(append_meta={'authorization.scope': 'DOMAIN'})
    @check_required(['collector_id', 'domain_id'])
    def enable(self, params):
        collector_mgr: CollectorManager = self.locator.get_manager(CollectorManager)
        collector_id = params['collector_id']
        domain_id = params['domain_id']
        return collector_mgr.enable_collector(collector_id, domain_id)

    @transaction(append_meta={'authorization.scope': 'DOMAIN'})
    @check_required(['collector_id', 'domain_id'])
    def disable(self, params):
        collector_mgr: CollectorManager = self.locator.get_manager(CollectorManager)
        collector_id = params['collector_id']
        domain_id = params['domain_id']
        return collector_mgr.disable_collector(collector_id, domain_id)

    @transaction(append_meta={'authorization.scope': 'DOMAIN'})
    @check_required(['domain_id'])
    @append_query_filter(['collector_id', 'name', 'state', 'priority', 'plugin_id', 'domain_id'])
    @append_keyword_filter(_KEYWORD_FILTER)
    def list(self, params):
        collector_mgr: CollectorManager = self.locator.get_manager(CollectorManager)
        return collector_mgr.list_collectors(params.get('query', {}))

    @transaction(append_meta={'authorization.scope': 'DOMAIN'})
    @check_required(['query', 'domain_id'])
    @append_query_filter(['domain_id'])
    @append_keyword_filter(_KEYWORD_FILTER)
    def stat(self, params):
        """
        Args:
            params (dict): {
                'domain_id': 'str',
                'query': 'dict (spaceone.api.core.v1.StatisticsQuery)'
            }

        Returns:
            values (list) : 'list of statistics data'

        """
        collector_mgr: CollectorManager = self.locator.get_manager(CollectorManager)
        return collector_mgr.stat_collectors(params.get('query', {}))

    @transaction(append_meta={'authorization.scope': 'DOMAIN'})
    @check_required(['collector_id', 'domain_id'])
    def collect(self, params):
        collector_mgr: CollectorManager = self.locator.get_manager(CollectorManager)
        collecting_mgr = self.locator.get_manager(CollectingManager)
        collector_plugin_mgr: CollectorPluginManager = self.locator.get_manager(CollectorPluginManager)
        plugin_mgr: PluginManager = self.locator.get_manager(PluginManager)
        job_mgr: JobManager = self.locator.get_manager(JobManager)
        job_task_mgr: JobTaskManager = self.locator.get_manager(JobTaskManager)

        collector_id = params['collector_id']
        domain_id = params['domain_id']
        collector_vo = collector_mgr.get_collector(collector_id, domain_id)
        collector_dict = collector_vo.to_dict()

        plugin_info = collector_dict['plugin_info']
        plugin_id = plugin_info['plugin_id']
        version = plugin_info['version']
        options = plugin_info.get('options', {})
        upgrade_mode = plugin_info.get('upgrade_mode', 'AUTO')

        endpoint, updated_version = plugin_mgr.get_endpoint(plugin_id, version, domain_id, upgrade_mode)

        if updated_version:
            _LOGGER.debug(f'[collect] upgrade plugin version: {version} -> {updated_version}')
            response = collector_plugin_mgr.init_plugin(endpoint, options)
            plugin_info.update({'version': updated_version, 'metadata': response['metadata']})
            collector_vo = collector_vo.update({'plugin_info': plugin_info})

            self.delete_collector_rules(collector_id, domain_id),
            self.create_collector_rules_by_metadata(response['metadata'], collector_id, domain_id)

        created_job = job_mgr.create_job(collector_vo, params)

        try:
            secret_ids = self.list_secret_from_secret_filter(plugin_info.get('secret_filters', {}),
                                                             params.get('secret_id'),
                                                             domain_id)

            if not secret_ids:
                # nothing to do
                job_mgr.make_success_by_vo(created_job)
                return created_job

        except Exception as e:
            _LOGGER.debug(f'[collect] failed in Secret Patch stage: {e}')
            job_mgr.add_error(created_job.job_id, domain_id, 'ERROR_COLLECT_INITIALIZE', e, params)
            job_mgr.make_error_by_vo(created_job)
            raise ERROR_COLLECT_INITIALIZE(stage='Secret Patch', params=params)

        try:
            job_mgr.make_inprogress_by_vo(created_job)
        except Exception as e:
            _LOGGER.debug(f'[collect] {e}')
            _LOGGER.debug(f'[collect] fail to change {collector_id} job state to in-progress')

        count = 0
        projects = []

        for secret_id in secret_ids:
            count += 1
            try:
                job_mgr.increase_total_tasks_by_vo(created_job)
                job_mgr.increase_remained_tasks_by_vo(created_job)

                secret_info = self._set_secret_info(secret_id, domain_id)

                if project_id := secret_info.get('project_id'):
                    projects.append(project_id)

                job_task_vo = job_task_mgr.create_job_task(created_job, secret_info, domain_id)

                req_params = collector_mgr.make_collecting_parameters(collector_dict=collector_dict,
                                                                      secret_id=secret_id,
                                                                      domain_id=domain_id,
                                                                      job_vo=created_job,
                                                                      job_task_vo=job_task_vo,
                                                                      params=params)

                task = collector_mgr.create_task_pipeline(req_params, domain_id)
                queue_name = collector_mgr.get_queue_name(name='collect_queue')

                if task and queue_name:
                    _LOGGER.warning(f'[collect] Asynchronous collect {count}/{len(secret_ids)}')
                    validate(task, schema=SPACEONE_TASK_SCHEMA)
                    json_task = json.dumps(task)
                    queue.put(queue_name, json_task)
                else:
                    _LOGGER.warning(f'[collect] Synchronous collect {count}/{len(secret_ids)}')
                    kwargs = {'job_id': req_params['job_id'], 'use_cache': req_params['use_cache']}
                    collecting_mgr.collecting_resources(req_params['plugin_info'],
                                                        req_params['secret_data'],
                                                        req_params['domain_id'],
                                                        **kwargs)

            except Exception as e:
                job_mgr.add_error(created_job.job_id, domain_id, 'ERROR_COLLECTOR_COLLECTING', e, {'secret_id': secret_id})
                _LOGGER.error(f'[collect] collect failed {count}/{len(secret_ids)}')
                _LOGGER.error(f'[collect] collecting failed with {secret_id}: {e}')

        # Update Timestamp
        collector_mgr.update_last_collected_time(collector_vo)
        return job_mgr.update_job_by_vo({'projects': list(set(projects))}, created_job)

    @transaction(append_meta={'authorization.scope': 'DOMAIN'})
    @check_required(['collector_id', 'domain_id'])
    def update_plugin(self, params):
        collector_mgr: CollectorManager = self.locator.get_manager(CollectorManager)
        collector_plugin_mgr: CollectorPluginManager = self.locator.get_manager(CollectorPluginManager)
        plugin_manager: PluginManager = self.locator.get_manager(PluginManager)

        collector_id = params['collector_id']
        domain_id = params['domain_id']

        collector_vo = collector_mgr.get_collector(collector_id, domain_id)
        plugin_info = collector_vo.plugin_info.to_dict()

        if version := params.get('version'):
            plugin_info['version'] = version

        if options := params.get('options'):
            plugin_info['options'] = options

        if upgrade_mode := params.get('upgrade_mode'):
            plugin_info['upgrade_mode'] = upgrade_mode

        endpoint, updated_version = plugin_manager.get_endpoint(plugin_info['plugin_id'],
                                                                plugin_info.get('version'),
                                                                domain_id,
                                                                plugin_info.get('upgrade_mode', 'AUTO'))

        plugin_response = collector_plugin_mgr.init_plugin(endpoint, plugin_info.get('options', {}))

        if updated_version:
            plugin_info['version'] = updated_version

        plugin_info['metadata'] = plugin_response.get('metadata', {})

        params = {'plugin_info': plugin_info}
        collector_vo = self.update_collector_by_vo(collector_vo, params)

        self.delete_collector_rules(collector_id, domain_id),
        self.create_collector_rules_by_metadata(plugin_info['metadata'], collector_id, domain_id)

        return collector_vo

    @transaction(append_meta={'authorization.scope': 'DOMAIN'})
    @check_required(['collector_id', 'domain_id'])
    def verify_plugin(self, params):
        collector_mgr: CollectorManager = self.locator.get_manager(CollectorManager)
        collector_plugin_mgr: CollectorPluginManager = self.locator.get_manager(CollectorPluginManager)
        plugin_manager: PluginManager = self.locator.get_manager(PluginManager)
        secret_manager: SecretManager = self.locator.get_manager(SecretManager)

        collector_id = params['collector_id']
        domain_id = params['domain_id']

        collector_vo = collector_mgr.get_collector(collector_id, domain_id)
        plugin_info = collector_vo.plugin_info.to_dict()

        endpoint, updated_version = plugin_manager.get_endpoint(plugin_info['plugin_id'],
                                                                plugin_info.get('version'),
                                                                domain_id,
                                                                plugin_info.get('upgrade_mode', 'AUTO'))

        secret_ids = self.list_secret_from_secret_filter(plugin_info.get('secret_filter', {}),
                                                         params.get('secret_id'),
                                                         domain_id)

        if secret_ids:
            secret_data_info = secret_manager.get_secret_data(secret_ids[0], domain_id)
            secret_data = secret_data_info.get('data', {})
            collector_plugin_mgr.verify_plugin(endpoint, plugin_info.get('options', {}), secret_data)

    def list_secret_from_secret_filter(self, secret_filter, secret_id, domain_id):
        secret_manager: SecretManager = self.locator.get_manager(SecretManager)

        _filter = self._set_secret_filter(secret_filter, secret_id)
        query = {'filter': _filter} if _filter else {}
        response = secret_manager.list_secrets(query, domain_id)

        return [secret_info.get('secret_id') for secret_info in response['results']]

    @check_required(['schedule'])
    def scheduled_collectors(self, params):
        """ Search all collectors in this schedule
        This is global search out-of domain

        Args:
            params(dict): {
                schedule(dict): {
                  'hours': list,
                  'minutes': list
                }
                domain_id: optional
            }
        ex) {'hour': 3}

        Returns: collectors_info
        """

        collector_mgr: CollectorManager = self.locator.get_manager(CollectorManager)

        filter_query = [{'k': 'collector.state', 'v': 'ENABLED', 'o': 'eq'}]

        if 'domain_id' in params:
            filter_query.append({'k': 'domain_id', 'v': params['domain_id'], 'o': 'eq'})

        schedule = params.get('schedule', {})
        if 'hour' in schedule:
            filter_query.append({'k': 'schedule.hours', 'v': schedule['hour'], 'o': 'contain'})
        elif 'minute' in schedule:
            filter_query.append({'k': 'schedule.minute', 'v': schedule['minute'], 'o': 'contain'})
        elif 'interval' in schedule:
            filter_query.append({'k': 'schedule.interval', 'v': 0, 'o': 'gt'})
        else:
            # TODO: CRON
            pass

        return collector_mgr.list_collectors({'filter': filter_query})

    def _get_plugin_from_repository(self, plugin_info, domain_id):
        repo_mgr: RepositoryManager = self.locator.get_manager(RepositoryManager)
        return repo_mgr.get_plugin(plugin_info['plugin_id'], domain_id)

    def create_collector_rules_by_metadata(self, plugin_metadata, domain_id, collector_id):
        collector_rule_mgr: CollectorRuleManager = self.locator.get_manager(CollectorRuleManager)
        collector_rules = plugin_metadata.get('collector_rules', [])

        for collector_rule_params in collector_rules:
            collector_rule_params.update({
                'domain_id': domain_id,
                'collector_id': collector_id,
                'rule_type': 'MANAGED'
            })

            collector_rule_mgr.create_collector_rule(collector_rule_params)

    def delete_collector_rules(self, collector_id, domain_id):
        collector_rule_mgr: CollectorRuleManager = self.locator.get_manager('CollectorRuleManager')
        old_collector_rule_vos = collector_rule_mgr.filter_collector_rules(collector_id=collector_id,
                                                                           rule_type='MANAGED',
                                                                           domain_id=domain_id)
        old_collector_rule_vos.delete()

    def _set_secret_info(self, secret_id, domain_id):
        secret_mgr: SecretManager = self.locator.get_manager('SecretManager')
        secret = secret_mgr.get_secret(secret_id, domain_id)
        secret_info = {'secret_id': secret_id}

        if provider := secret.get('provider'):
            secret_info.update({'provider': provider})
        if service_account_id := secret.get('service_account_id'):
            secret_info.update({'service_account_id': service_account_id})

        if project_id := secret.get('project_id'):
            secret_info.update({'project_id': project_id})

        return secret_info

    @staticmethod
    def _set_secret_filter(secret_filter, secret_id):
        _filter = []
        if 'secrets' in secret_filter:
            _filter.append({'k': 'secret_id', 'v': secret_filter['secrets'], 'o': 'in'})
        if 'service_accounts' in secret_filter:
            _filter.append({'k': 'service_account_id', 'v': secret_filter['service_accounts'], 'o': 'in'})
        if 'schemas' in secret_filter:
            _filter.append({'k': 'schema', 'v': secret_filter['schemas'], 'o': 'in'})
        if secret_id:
            _filter.append({'k': 'secret_id', 'v': secret_id, 'o': 'eq'})

        return _filter

    @staticmethod
    def _get_plugin_providers(provider, plugin_info):
        supported_providers = plugin_info.get('capability', {}).get('supported_providers', [])

        if supported_providers:
            # Multi providers
            if provider in supported_providers:
                return provider
            else:
                raise ERROR_INVALID_PARAMETER(key='provider', reason=f'Not supported provider: {provider}')
        else:
            # Single provider
            return provider if provider else plugin_info.get('provider')

    @staticmethod
    def _convert_tags(tags):
        if isinstance(tags, list):
            return utils.tags_to_dict(tags)
        else:
            return tags
