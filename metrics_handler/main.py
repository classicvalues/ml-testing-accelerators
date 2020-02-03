import base64
from collections import defaultdict
from collections import namedtuple
import datetime
import io
import json
import math
import time

from absl import logging
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
import google.api_core.exceptions
from google.cloud import bigquery
from google.cloud import monitoring_v3
from google.cloud import storage as gcs
from google.cloud.monitoring_v3.proto import alert_pb2
import numpy as np
from tensorboard.backend.event_processing import event_multiplexer
import tensorflow as tf

from google.protobuf import duration_pb2

_SUCCESS_CODE = 0.0
_FAILURE_CODE = 1.0
_TIMEOUT_CODE = 2.0
_JOB_STATUS = 'job_status'

_60_SECOND_DURATION = duration_pb2.Duration()
_60_SECOND_DURATION.FromTimedelta(datetime.timedelta(seconds=60))
_5_MINUTE_DURATION = duration_pb2.Duration()
_5_MINUTE_DURATION.FromTimedelta(datetime.timedelta(minutes=5))
_BASE_ALERT_DICT = {
  'display_name': 'REPLACE_ME',
  'combiner': 'OR',
  'conditions': [
      {
          'display_name': 'Metric is outside the expected value.',
          'condition_threshold': {
              'aggregations': [
                  {
                      'alignment_period': _60_SECOND_DURATION,
                      'cross_series_reducer': 'REDUCE_MEAN',
                      'group_by_fields': [
                          'project',
                          'resource.label.instance_id',
                          'resource.label.zone'
                      ],
                      'per_series_aligner': 'ALIGN_MAX'
                  }
              ],
              'duration': _5_MINUTE_DURATION,
              'trigger': {
                  'count': 1
              },
              'comparison': 'REPLACE_ME',
              'filter': 'REPLACE_ME',
              'threshold_value': 'REPLACE_ME'
          }
      }
  ],
}


class CloudMetricsHandler(object):
  def __init__(self, test_name, events_dir, stackdriver_logs_link,
               metric_collection_config, regression_alert_config,
               job_name):
    """Handles metrics storage, collection, aggregation, alerts, etc.

    Used in conjunction with the Cloud Accelerator Testing framework
    (TODO: link to top-level Github repo for the framework).

    Args:
      test_name (string): This should be a string that uniquely identifies
        a test among all your other Cloud Accelerator tests. Used for some
        naming, e.g. for the name of alerts if this test's metrics regress.
      events_dir (string): Path to a GCS or local directory where Tensorboard
        summaries are stored.
      stackdriver_logs_link (string): Link to the Stackdriver Logs for the run
        that produced the metrics being handled.
      metric_collection_config (dict): Options for collecting metrics. See
        README for documentation.
      regression_alert_config (dict): Options for alerting in the event of
        metrics regressions. See README for documentation.
      job_name (string): Name of the Kubernetes Job that ran this instance
        of the test.
    """
    self.MetricPoint = namedtuple('MetricPoint', 'metric_value wall_time')
    self.test_name = test_name
    self.events_dir = events_dir
    self.stackdriver_logs_link = stackdriver_logs_link
    self.job_name = job_name
    self.metric_collection_config = metric_collection_config or {}
    self.regression_alert_config = regression_alert_config or {}
    if self.regression_alert_config.get('metric_opt_in_list', None):
      self.regression_metrics = set(
          self.regression_alert_config['metric_opt_in_list'])
    else:
      self.regression_metrics = None

    # Initalize clients to interact with various Cloud APIs.
    self.project = google.auth.default()[1]
    self.bigquery_client = bigquery.Client()
    self.gcs_client = gcs.Client()
    self.monitoring_client = monitoring_v3.MetricServiceClient()
    self.alert_client = monitoring_v3.AlertPolicyServiceClient()
    self.notification_client = google.cloud.monitoring_v3.NotificationChannelServiceClient()


  @staticmethod
  def _wall_time_to_sql_timestamp(wall_time):
    return datetime.datetime.fromtimestamp(wall_time).strftime(
        '%Y-%m-%d %H:%M:%S')


  def _get_table_id(self, dataset_name, table_name):
     return '{}.{}.{}'.format(self.project, dataset_name, table_name)


  def _metric_name_to_alert_display_name(self, metric_name):
    return 'MetricOutsideExpectedBounds__TestName:{}__MetricName:{}'.format(
        self.test_name, metric_name)


  def _metric_name_to_metric_id(self, metric_name):
    return '{}/{}/{}'.format(
        'custom.googleapis.com/cloudtpu', self.test_name, metric_name)


  def _aggregate_metrics(self, metrics, strategy):
    """Aggregate a list of MetricPoint namedtuples based on given strategy."""
    if strategy == 'final':
      # Take the MetricPoint with the latest wall_time.
      latest_metric = metrics[0]
      for metric in metrics[1:]:
        if metric.wall_time > latest_metric.wall_time:
          latest_metric = metric
      return latest_metric
    elif strategy == 'max':
      # Take the MetricPoint with the maximum metric value.
      max_metric = metrics[0]
      for metric in metrics[1:]:
        if metric.metric_value > max_metric.metric_value:
          max_metric = metric
      return max_metric
    elif strategy == 'min':
      # Take the MetricPoint with the minimum metric value.
      min_metric = metrics[0]
      for metric in metrics[1:]:
        if metric.metric_value < min_metric.metric_value:
          min_metric = metric
      return min_metric
    else:
      raise ValueError('Unknown aggregation strategy: {}'.format(strategy))


  def _add_time_to_accuracy_to_metrics(self, raw_metrics, metrics_to_update):
    """Compute time_to_accuracy based on `raw_metrics`."""
    tta_config = self.metric_collection_config['time_to_accuracy']
    if 'accuracy_tag' not in tta_config or \
        'accuracy_threshold' not in tta_config:
      raise ValueError('Invalid `time_to_accuracy` portion of config. '
                       'See README for how to set up the config.')
    values = raw_metrics.get(tta_config['accuracy_tag'], [])
    if not values:
      raise ValueError('No values found for time to accuracy tag: {}. '
          'Possible tags were: {}'.format(
          tta_config['accuracy_tag'], raw_metrics.keys()))

    # MetricPoints should be sorted by timestamp with earlier events first.
    start_wall_time = values[0].wall_time
    end_wall_time = -1
    for v in values:
      if v.metric_value >= tta_config['accuracy_threshold']:
        end_wall_time = v.wall_time
        break
    if end_wall_time > 0:
      metrics_to_update['time_to_accuracy'] = self.MetricPoint(
          metric_value=(end_wall_time - start_wall_time),
          wall_time=end_wall_time)
    else:
      logging.error('Accuracy was never high enough to satisfy the '
                    '`time_to_accuracy` settings from the config.')
      metrics_to_update['time_to_accuracy'] = self.MetricPoint(
          # Set to a high enough value to trigger alerts.
          metric_value=60 * 60 * 24 * 365,
          wall_time=start_wall_time)


  def _add_total_wall_time_to_metrics(self, raw_metrics, metrics_to_update):
    """Compute total time according to `raw_metrics`."""
    if not raw_metrics:
      logging.warning('Empty raw_metrics; skipping total_wall_time')
      return
    min_wall_time = math.inf
    max_wall_time = -math.inf
    for tag in raw_metrics:
      for value in raw_metrics[tag]:
        if value.wall_time < min_wall_time:
          min_wall_time = value.wall_time
        if value.wall_time > max_wall_time:
          max_wall_time = value.wall_time
    metrics_to_update['total_wall_time'] = self.MetricPoint(
        metric_value=(max_wall_time - min_wall_time),
        wall_time=max_wall_time)


  def _add_job_status_to_metrics(self, final_metrics):
    # Retrieve Job status from Kubernetes.
    try:
      # Used when running in a Cloud Function.
      k8s_config.load_incluster_config()
      logging.info('Successfully loaded k8s config')
    except Exception as e:
      # Used when running locally.
      logging.error('k8s load_incluster_config failed with: {}'.format(e))
      k8s_config.load_kube_config()
    kubernetes_client = k8s_client.BatchV1Api()
    status = None
    for namespace in ['default', 'automated']:
      try:
        status = kubernetes_client.read_namespaced_job_status(
          self.job_name, namespace).status
        break
      except k8s_client.rest.ApiException:
        continue
    if not status:
      raise ValueError('Could not find status for k8s job: {}'.format(
          self.job_name))

    # Interpret status and add to metrics.
    # TODO: status.completion_time isn't populated sometimes (maybe if one
    # attempt of the job timed out?) so use start_time as a back up.
    if status.completion_time:
      completion_time = status.completion_time.timestamp()
    else:
      completion_time = status.start_time.timestamp()
    if status.succeeded:
      final_metrics[_JOB_STATUS] = self.MetricPoint(
          metric_value=_SUCCESS_CODE,
          wall_time=completion_time)
    else:
      found_timeout = False
      for condition in status.conditions:
        if condition.reason == 'DeadlineExceeded':
          found_timeout = True
          break
      final_metrics[_JOB_STATUS] = self.MetricPoint(
          metric_value=_TIMEOUT_CODE if found_timeout else _FAILURE_CODE,
          wall_time=completion_time)


  def get_metrics_from_events_dir(self):
    tags_to_ignore = set(
        self.metric_collection_config.get('tags_to_ignore', []))

    em = event_multiplexer.EventMultiplexer()
    em.AddRunsFromDirectory(self.events_dir)
    em.Reload()

    # First pass: collect the values for each metric.
    raw_metrics = defaultdict(list)
    for run, tags in em.Runs().items():
      # 'Old-style' runs have a simple format and store values directly.
      for tag in tags['scalars']:
        if tag in tags_to_ignore:
          continue
        raw_metrics[tag].extend(
            [self.MetricPoint(metric_value=x.value, wall_time=x.wall_time)
            for x in em.Scalars(run, tag)])
      # 'New-style' runs stores values inside of Tensor protos.
      for tag in tags['tensors']:
        if tag in tags_to_ignore:
          continue
        for t in em.Tensors(run, tag):
          tensor_dtype = tf.dtypes.as_dtype(t.tensor_proto.dtype)
          try:
            val = np.frombuffer(
                t.tensor_proto.tensor_content,
                tensor_dtype.as_numpy_dtype).tolist()
            assert len(val) == 1  # There should be 1 value per tensor.
            raw_metrics[tag].append(
                self.MetricPoint(metric_value=val[0], wall_time=t.wall_time))
          except ValueError as e:
            logging.error(
                'Unable to parse tag: `{}` from tensor_content: {}. '
                'Error: {}. Consider adding this tag to tags_to_ignore '
                'in config.'.format(tag, t.tensor_proto.tensor_content, e))

    # Second pass: aggregate values for each metric based on the config.
    final_metrics = {}
    tag_to_custom_aggregation_strategies = self.metric_collection_config.get(
        'metric_to_aggregation_strategies', {})
    for tag, metrics in raw_metrics.items():
      strategies = tag_to_custom_aggregation_strategies.get(
          tag, self.metric_collection_config['default_aggregation_strategies'])
      for strategy in strategies:
        final_metrics['{}_{}'.format(tag, strategy)] = self._aggregate_metrics(
            metrics, strategy)

    # Compute time_to_accuracy if requested in the config.
    if 'time_to_accuracy' in self.metric_collection_config:
      self._add_time_to_accuracy_to_metrics(raw_metrics, final_metrics)

    self._add_total_wall_time_to_metrics(raw_metrics, final_metrics)

    # TODO: Re-enable this once kube config is figured out.
    # https://b.corp.google.com/issues/148563355#comment11
    # self._add_job_status_to_metrics(final_metrics)
    return final_metrics


  def _make_bigquery_table(self):
    if not self.metric_collection_config.get('write_to_bigquery'):
      return
    dataset_name = self.metric_collection_config['bigquery_dataset_name']
    dataset = bigquery.Dataset(self.bigquery_client.dataset(dataset_name))
    _ = self.bigquery_client.create_dataset(dataset, exists_ok=True)

    table_id = self._get_table_id(
        dataset_name, self.metric_collection_config['bigquery_table_name'])
    schema = [
        bigquery.SchemaField("test_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("metric_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("metric_value", "FLOAT64", mode="REQUIRED"),
        bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("stackdriver_logs_link", "STRING",
                             mode="REQUIRED"),
        bigquery.SchemaField("metric_upper_bound", "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("metric_lower_bound", "FLOAT64", mode="NULLABLE"),
    ]
    table = bigquery.Table(table_id, schema=schema)
    _ = self.bigquery_client.create_table(table, exists_ok=True)
    return table_id


  def add_new_metrics_to_bigquery(self, aggregated_metrics_dict):
    if not self.metric_collection_config.get('write_to_bigquery', False):
      logging.info('Skipping writing metrics to BigQuery.')
      return
    if not aggregated_metrics_dict:
      logging.warning('No metrics to write to BigQuery.')
      return
    table_id = self._make_bigquery_table()
    # TODO: Compute metric_upper_bound and metric_lower_bound and pass
    # those instead of None, None.
    rows_to_insert = [
        (self.test_name, key, float(x.metric_value),
        self._wall_time_to_sql_timestamp(x.wall_time),
        self.stackdriver_logs_link, None, None) for key, x in \
        aggregated_metrics_dict.items()]
    logging.info('Rows to insert into BigQuery: {}'.format(rows_to_insert))
    table = self.bigquery_client.get_table(table_id)
    errors = self.bigquery_client.insert_rows(table, rows_to_insert)
    if errors == []:
      logging.info('Added metrics to bigquery table: {}'.format(table_id))
    else:
      # TODO: Maybe add retry logic. insert_rows seems to be atomic for all
      #       elements in the list, so it should be safe to retry.
      logging.error(
          'Failed to add metrics to bigquery table: {}'.format(errors))


  def add_new_metrics_to_stackdriver(self, new_metrics_dict):
    if not self.regression_alert_config.get('write_to_stackdriver',
                                            False):
      logging.info('Skipping writing metrics to Stackdriver.')
      return
    project_name = self.monitoring_client.project_path(self.project)
    series_list = []
    for metric_name, metric_point in new_metrics_dict.items():
      if self.regression_metrics and metric_name not in self.regression_metrics:
        continue
      series = monitoring_v3.types.TimeSeries()
      series.metric.type = self._metric_name_to_metric_id(metric_name)
      # Resource type, instance_id, and zone are required by monitoring API
      # even though they are irrelevant to our metrics.
      series.resource.type = 'k8s_pod'
      series.resource.labels['cluster_name'] = 'xl-ml-test'
      series.resource.labels['location'] = 'us-central1-b'
      series.resource.labels['pod_name'] = 'xl-ml-test'
      series.resource.labels['namespace_name'] = 'xl-ml-test'
      point = series.points.add()
      point.value.double_value = metric_point.metric_value
      point.interval.end_time.seconds = int(metric_point.wall_time)
      point.interval.end_time.nanos = int(
          (metric_point.wall_time - point.interval.end_time.seconds) * 10**9)
      series_list.append(series)
    self.monitoring_client.create_time_series(project_name, series_list)
    logging.info('Added metrics to stackdriver.')



  def _get_metrics_history_from_bigquery(self):
    dataset_name = self.regression_alert_config['bigquery_dataset_name']
    table_id = self._get_table_id(
        dataset_name, self.regression_alert_config['bigquery_table_name'])
    rows_iter = self.bigquery_client.list_rows(table_id)
    metrics_history = defaultdict(list)
    for row in rows_iter:
      if row['test_name'] != self.test_name:
        continue
      metrics_history[row['metric_name']].append(self.MetricPoint(
          metric_value=row['metric_value'],
          wall_time=row['timestamp'].timestamp()))
    return metrics_history


  def _get_notification_channels(self):
    notification_channels = []
    if 'notification_channel_display_names' in self.regression_alert_config:
      display_names_to_find = set(
        self.regression_alert_config['notification_channel_display_names'])
      project_name = self.notification_client.project_path(self.project)
      for nc in self.notification_client.list_notification_channels(
          project_name):
        if nc.display_name in display_names_to_find:
          notification_channels.append(nc.name)
          display_names_to_find.remove(nc.display_name)
          if not display_names_to_find:
            return notification_channels

      # If we checked all existing notification channels and didn't find all
      # that the user requested in the config, log a warning.
      logging.error(
          'No notification channel found for display_names: {}. '
          'You can create channels in the Pantheon UI under Stackdriver > '
          'Monitoring > Alerting > Edit Notification Channels'.format(
              display_names_to_find))
    else:
      logging.warning(
          'No notification channels set; no emails will be sent '
          'for firing alerts. See the config documentation for how to '
          'set these up.')
    return notification_channels


  def _compute_alert_bounds(self, new_metrics):
    metrics_history = self._get_metrics_history_from_bigquery()

    # Add the metrics from the latest run. These aren't in Bigquery yet.
    for metric_name, metric_value in new_metrics.items():
      metrics_history[metric_name].append(metric_value)

    notification_channels = self._get_notification_channels()
    metric_name_to_alert = {}
    metrics_to_ignore = set(self.regression_alert_config.get(
        'metrics_to_ignore', []))
    for metric_name, metric_value_list in metrics_history.items():
      if metric_name in metrics_to_ignore:
        continue
      metric_values = [v.metric_value for v in metric_value_list]
      if len(metric_values) < self.regression_alert_config[
          'min_num_datapoints_before_alerting']:
        continue
      v_mean = np.mean(metric_values)
      v_stddev = np.std(metric_values)
      threshold_expr = self.regression_alert_config.get(
          'threshold_expression_overrides', {}).get(metric_name) or \
          self.regression_alert_config['base_threshold_expression']
      regression_threshold = eval(threshold_expr, None, {
          'v_mean': v_mean, 'v_stddev': v_stddev})
      regression_comparison = self.regression_alert_config.get(
          'comparison_overrides', {}).get(metric_name) or \
          self.regression_alert_config['base_comparison']

      # Create the Stackdriver AlertPolicy based on the bounds.
      new_alert = dict(_BASE_ALERT_DICT)
      new_alert['display_name'] = self._metric_name_to_alert_display_name(
          metric_name)
      new_alert['conditions'][0]['condition_threshold']['comparison'] = \
          regression_comparison
      new_alert['conditions'][0]['condition_threshold']['threshold_value'] = \
          regression_threshold
      new_alert['conditions'][0]['condition_threshold']['filter'] = \
          "metric.type=\"{}\" AND resource.type=\"gce_instance\"".format(
              self._metric_name_to_metric_id(metric_name))
      alert_policy = alert_pb2.AlertPolicy(**new_alert)
      if notification_channels:
        alert_policy.notification_channels[:] = notification_channels
      metric_name_to_alert[metric_name] = alert_policy

    return metric_name_to_alert


  def add_alerts_to_stackdriver(self, new_metrics):
    if not self.regression_alert_config.get('write_to_stackdriver',
                                            False):
      logging.info('Skipping writing alerts to Stackdriver.')
      return
    metric_name_to_alert_dict = self._compute_alert_bounds(new_metrics)
    project_name = self.alert_client.project_path(self.project)

    # First find the unique ID for all the existing policies.
    # The ID is required to update existing policies.
    display_name_to_alert_id = {}
    for p in self.alert_client.list_alert_policies(project_name):
      display_name_to_alert_id[p.display_name] = p.name

    # For each bound that we computed, update or create a corresponding policy.
    for metric_name, alert in metric_name_to_alert_dict.items():
      if self.regression_metrics and metric_name not in self.regression_metrics:
        continue
      display_name = self._metric_name_to_alert_display_name(metric_name)
      if display_name in display_name_to_alert_id:
        alert.name = display_name_to_alert_id[display_name]
        response = self.alert_client.update_alert_policy(alert)
      else:
        response = self.alert_client.create_alert_policy(project_name, alert)
    logging.info('Added alerts to Stackdriver.')


def run_main(event, context):
  logging.info('context: {}'.format(context))
  logging.info('Raw pubsub message: {}'.format(event['data']))
  pubsub_message = base64.b64decode(event['data']).decode('utf-8')
  event = json.loads(pubsub_message)
  logging.info('Decoded pubsub message: {}'.format(event))

  events_dir = event.get('model_dir')
  test_name = event.get('test_name')
  logs_link = event.get('logs_link')
  metric_collection_config = event.get('metric_collection_config')
  regression_alert_config = event.get('regression_test_config')
  job_name = event.get('job_name')

  if not regression_alert_config and not metric_collection_config:
    raise ValueError('metric_collection_config and regression_alert_config '
                     'were both null; stopping early. See README for '
                     'documentation on writing these configs.')
  if not (events_dir and test_name and logs_link and job_name):
    raise ValueError('Pubsub message must contain 4 required fields: '
                     'events_dir, test_name, logs_link, and job_name. See '
                     'README for documentation. Message was: {}'.format(event))
  handler = CloudMetricsHandler(test_name, events_dir, logs_link,
      metric_collection_config, regression_alert_config, job_name)

  new_metrics = handler.get_metrics_from_events_dir()
  logging.info('new_metrics: {}\n\n\n'.format(new_metrics))

  handler.add_new_metrics_to_bigquery(new_metrics)
  # TODO: this once threw google.api_core.exceptions.DeadlineExceeded: 504 Deadline Exceeded

  handler.add_new_metrics_to_stackdriver(new_metrics)
  # TODO: this once threw google.api_core.exceptions.InternalServerError
  #   ^^ consider retrying for some statuses: https://github.com/grpc/grpc/blob/master/doc/statuscodes.md

  handler.add_alerts_to_stackdriver(new_metrics)

