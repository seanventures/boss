"""BOSSAPP-1293: the CloudWatch connector — metrics (GetMetricData), logs (Logs Insights and
FilterLogEvents) and alarms (DescribeAlarms) on the same TelemetrySource contract as Prometheus
and Loki, READ-ONLY by construction. Real boto3 clients with botocore's Stubber: request shapes
are validated against the AWS service model, nothing reaches the network."""
from __future__ import annotations

from datetime import datetime, timezone

import boto3
import pytest
from botocore.stub import ANY, Stubber

from boss_engine.services import telemetry_connectors as tc
from boss_engine.services.reliability import build_source
from boss_engine.services.telemetry_source import TimeWindow

WIN = TimeWindow(1_700_000_000, 1_700_000_900)


@pytest.fixture
def aws(monkeypatch):
    """Stubbed cloudwatch + logs clients (fake static creds so botocore can sign offline)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    clients = {name: boto3.client(name, region_name="us-east-1") for name in ("cloudwatch", "logs")}
    stubs = {name: Stubber(client) for name, client in clients.items()}
    for stub in stubs.values():
        stub.activate()
    yield clients, stubs
    for stub in stubs.values():
        stub.assert_no_pending_responses()
        stub.deactivate()


def _source(clients, **kw):
    return tc.CloudWatchSource("/aws/apigw/orders", client_factory=lambda s: clients[s], **kw)


def test_metrics_read_through_get_metric_data_with_paging(aws):
    clients, stubs = aws
    t1, t2 = (datetime.fromtimestamp(WIN.start + 60 * i, tz=timezone.utc) for i in (1, 2))
    stubs["cloudwatch"].add_response(
        "get_metric_data",
        {"MetricDataResults": [{"Id": "q0", "Label": "5XXError", "Timestamps": [t2],
                                "Values": [7.0], "StatusCode": "PartialData"}],
         "NextToken": "page2"},
        {"MetricDataQueries": [{"Id": "q0", "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": "AWS/ApiGateway", "MetricName": "5XXError",
                       "Dimensions": [{"Name": "ApiName", "Value": "orders"}]},
            "Period": 60, "Stat": "Sum"}}],
         "StartTime": ANY, "EndTime": ANY, "ScanBy": "TimestampAscending"})
    stubs["cloudwatch"].add_response(
        "get_metric_data",
        {"MetricDataResults": [{"Id": "q0", "Label": "5XXError", "Timestamps": [t1],
                                "Values": [2.0], "StatusCode": "Complete"}]},
        {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY,
         "ScanBy": "TimestampAscending", "NextToken": "page2"})
    series = _source(clients).query_metrics("AWS/ApiGateway 5XXError Sum ApiName=orders", WIN)
    assert len(series) == 1 and series[0].name == "5XXError"
    assert [(p.ts, p.value) for p in series[0].points] == [(t1.timestamp(), 2.0),
                                                           (t2.timestamp(), 7.0)]


def test_metric_query_forms():
    assert tc.parse_metric_query("SELECT AVG(CPUUtilization) FROM SCHEMA(\"AWS/EC2\")", 60)[0][
        "Expression"].startswith("SELECT")
    assert tc.parse_metric_query('[{"Id": "a", "Expression": "m1*2"}]', 60) == [
        {"Id": "a", "Expression": "m1*2"}]
    stat = tc.parse_metric_query("AWS/Lambda Errors", 300)[0]["MetricStat"]
    assert stat["Stat"] == "Average" and stat["Period"] == 300
    with pytest.raises(ValueError):
        tc.parse_metric_query("justone", 60)


def test_alarms_in_alarm_state_become_alerts(aws):
    clients, stubs = aws
    when = datetime.fromtimestamp(WIN.end - 30, tz=timezone.utc)
    stubs["cloudwatch"].add_response(
        "describe_alarms",
        {"MetricAlarms": [{"AlarmName": "orders-5xx", "StateValue": "ALARM",
                           "AlarmArn": "arn:aws:cloudwatch:us-east-1:111122223333:alarm:orders-5xx",
                           "StateUpdatedTimestamp": when, "Namespace": "AWS/ApiGateway",
                           "Dimensions": [{"Name": "ApiName", "Value": "orders"}]}]},
        {"StateValue": "ALARM", "MaxRecords": 100})
    alerts = _source(clients).list_alerts(WIN)
    assert [(a.title, a.service, a.severity, a.ts) for a in alerts] == [
        ("orders-5xx", "orders", "critical", when.timestamp())]


def test_an_insights_query_runs_through_start_query_and_polls(aws, monkeypatch):
    clients, stubs = aws
    monkeypatch.setattr(tc, "CW_SLEEP", lambda s: None)
    q = "fields @timestamp, @message | filter @message like /timeout/"
    stubs["logs"].add_response(
        "start_query", {"queryId": "qid-1"},
        {"logGroupNames": ["/aws/apigw/orders"], "queryString": q,
         "startTime": int(WIN.start), "endTime": int(WIN.end), "limit": 50})
    stubs["logs"].add_response("get_query_results", {"status": "Running", "results": []},
                               {"queryId": "qid-1"})
    stubs["logs"].add_response(
        "get_query_results",
        {"status": "Complete", "results": [[
            {"field": "@timestamp", "value": "2023-11-14 22:15:00.000"},
            {"field": "@message", "value": "upstream timeout"},
            {"field": "@ptr", "value": "x"}]]},
        {"queryId": "qid-1"})
    events = _source(clients).query_logs(q, WIN, limit=50)
    assert [e.message for e in events] == ["upstream timeout"]
    assert events[0].ts == datetime(2023, 11, 14, 22, 15, tzinfo=timezone.utc).timestamp()
    assert "@ptr" not in events[0].fields


def test_a_filter_pattern_uses_filter_log_events(aws):
    clients, stubs = aws
    stubs["logs"].add_response(
        "filter_log_events",
        {"events": [{"timestamp": 1_700_000_100_000, "message": "ERROR 502",
                     "logStreamName": "stage-prod"}]},
        {"logGroupName": "/aws/apigw/orders", "startTime": int(WIN.start * 1000),
         "endTime": int(WIN.end * 1000), "limit": 100, "filterPattern": "ERROR"})
    events = _source(clients).query_logs("ERROR", WIN, limit=100)
    assert events[0].message == "ERROR 502" and events[0].ts == 1_700_000_100.0


def test_the_connection_is_read_only_whatever_the_code_asks(aws):
    clients, _stubs = aws
    src = _source(clients)
    with pytest.raises(tc.CloudWatchReadOnly):
        src._client("cloudwatch").put_metric_alarm
    with pytest.raises(tc.CloudWatchReadOnly):
        src._client("logs").delete_log_group
    assert {op for ops in tc.CLOUDWATCH_READ_OPERATIONS.values() for op in ops} == {
        "get_metric_data", "describe_alarms", "filter_log_events", "start_query",
        "get_query_results"}


def test_credentials_are_named_env_vars_and_fail_closed(monkeypatch):
    monkeypatch.delenv("CW_KEY", raising=False)
    with pytest.raises(tc.ConnectorNotReady):
        tc.ensure_connector_ready({"kind": "cloudwatch", "access_key_env": "CW_KEY"})
    with pytest.raises(tc.ConnectorNotReady):
        tc.ensure_connector_ready({"kind": "cloudwatch", "access_key_env": "CW_KEY",
                                   "secret_key_env": "CW_SECRET"})
    monkeypatch.setenv("CW_KEY", "x")
    monkeypatch.setenv("CW_SECRET", "y")
    tc.ensure_connector_ready({"kind": "cloudwatch", "access_key_env": "CW_KEY",
                               "secret_key_env": "CW_SECRET"})
    tc.ensure_connector_ready({"kind": "cloudwatch"})          # the AWS chain (a role)
    src = build_source({"kind": "cloudwatch", "region": "eu-west-1", "access_key_env": "CW_KEY",
                        "secret_key_env": "CW_SECRET"})
    assert src.kind == "cloudwatch" and src.region == "eu-west-1"
    catalog = {c["kind"]: c for c in tc.list_connector_catalog()}["cloudwatch"]
    assert set(catalog["capabilities"]) == {"metrics", "logs", "alerts"} and catalog["read_only"]
    assert set(catalog["iam_actions"]) == set(tc.CLOUDWATCH_IAM_ACTIONS)


@pytest.mark.parametrize("name", ["BOSS_GITHUB_TOKEN", "DATABASE_URL", "ANTHROPIC_API_KEY",
                                  "aws_access_key_id", "AWS", "PATH"])
def test_a_key_env_must_name_an_aws_or_cloudwatch_secret(name, monkeypatch):
    """PR #330 review: a connection may not point the AWS client at another credential."""
    from boss_engine.services.reliability import build_source
    from boss_engine.services.telemetry_connectors import (
        CloudWatchSource, ConnectorNotReady, ensure_connector_ready)

    monkeypatch.setenv(name, "x" * 20)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y" * 20)
    spec = {"kind": "cloudwatch", "log_group": "/app", "access_key_env": name,
            "secret_key_env": "AWS_SECRET_ACCESS_KEY"}
    with pytest.raises(ConnectorNotReady) as refused:
        ensure_connector_ready(spec)
    assert refused.value.status_code == 422 and "not allowed" in str(refused.value)
    with pytest.raises(ConnectorNotReady):
        build_source(spec)
    with pytest.raises(ConnectorNotReady):
        CloudWatchSource("/app", session_token_env=name)


@pytest.mark.parametrize("name", ["AWS_ACCESS_KEY_ID", "CW_ACCESS_KEY_ID", "CLOUDWATCH_KEY"])
def test_aws_and_cloudwatch_key_envs_are_accepted(name, monkeypatch):
    from boss_engine.services.telemetry_connectors import ensure_connector_ready

    monkeypatch.setenv(name, "x" * 20)
    monkeypatch.setenv("CW_SECRET_ACCESS_KEY", "y" * 20)
    ensure_connector_ready({"kind": "cloudwatch", "log_group": "/app", "access_key_env": name,
                            "secret_key_env": "CW_SECRET_ACCESS_KEY"})
