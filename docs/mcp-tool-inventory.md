# Grafana MCP — Tool Inventory

All **73 tools** exposed by the self-hosted Grafana MCP server ([grafana/mcp-grafana](https://github.com/grafana/mcp-grafana), streamable HTTP) against the DEAD AIR stack at `deadair.grafana.net`. This inventory drives the design of the DEAD AIR operations agent: which tools it gets, which it is denied, and which need wrapping.

- **Enumerated:** 2026-08-17
- **Server:** `grafana/mcp-grafana:latest` @ `sha256:f21a19ce…` in `-t streamable-http` mode (see [docker-compose.yml](../docker-compose.yml))
- **Endpoint:** `http://localhost:8010/mcp`

Parameters marked `*` are required.

## Prometheus (Metrics) (6)

| Tool | Parameters | Notes |
| --- | --- | --- |
| `list_prometheus_label_names` | `datasourceUid`*, `endRfc3339`, `limit`, `matches`, `projectName`, `startRfc3339` | List label names in a PromQL-compatible datasource (Prometheus, Thanos, Mimir, Cloud Monitoring, etc.). |
| `list_prometheus_label_values` | `datasourceUid`*, `labelName`*, `endRfc3339`, `limit`, `matches`, `projectName`, `startRfc3339` | Use after list_prometheus_metric_names to find label values for filtering queries. |
| `list_prometheus_metric_metadata` | `datasourceUid`*, `limit`, `limitPerMetric`, `metric`, `projectName` | List Prometheus metric metadata. Returns metadata about metrics currently scraped from targets. |
| `list_prometheus_metric_names` | `datasourceUid`*, `endRfc3339`, `limit`, `page`, `projectName`, `regex`, `startRfc3339` | DISCOVERY: Call this first to find available metrics before querying. |
| `query_prometheus` | `datasourceUid`*, `endTime`*, `expr`*, `projectName`, `queryType`, `startTime`, `stepSeconds` | WORKFLOW: list_prometheus_metric_names -> list_prometheus_label_values -> query_prometheus. |
| `query_prometheus_histogram` | `datasourceUid`*, `metric`*, `percentile`*, `endTime`, `labels`, `projectName`, `rateInterval`, `startTime`, `stepSeconds` | Query Prometheus histogram percentiles. DISCOVER FIRST: Use list_prometheus_metric_names with regex='.*_bucket$' to find histograms. |

## Loki (Logs) & Analysis (8)

| Tool | Parameters | Notes |
| --- | --- | --- |
| `analyze_loki_labels` | `datasourceUid`, `endRfc3339`, `expectedBaseLabels`, `labels`, `maxLabels`, `perfMetrics`, `selector`, `startRfc3339` | Audits a Loki label strategy and optionally diagnoses query performance. |
| `find_error_pattern_logs` | `labels`*, `name`*, `end`, `start` | Searches Loki logs for elevated error patterns compared to the last day's average, waits for the analysis to complete, and returns the results… |
| `list_loki_label_names` | `datasourceUid`*, `endRfc3339`, `startRfc3339` | Lists all available label/field names (keys) found in logs within a specified Loki or VictoriaLogs datasource and time range. |
| `list_loki_label_values` | `datasourceUid`*, `labelName`*, `endRfc3339`, `startRfc3339` | Retrieves all unique values associated with a specific `labelName` within a Loki or VictoriaLogs datasource and time range. |
| `query_loki_logs` | `datasourceUid`*, `logql`*, `direction`, `endRfc3339`, `limit`, `queryType`, `startRfc3339`, `stepSeconds` | Executes a log query against a Loki or VictoriaLogs datasource and returns matching log entries (or metric samples on Loki). |
| `query_loki_patterns` | `datasourceUid`*, `logql`*, `endRfc3339`, `startRfc3339`, `step` | Retrieves detected log patterns from a Loki datasource for a given stream selector and time range. |
| `query_loki_stats` | `datasourceUid`*, `logql`*, `endRfc3339`, `startRfc3339` | Retrieves index-level statistics about log streams matching a given selector within a Loki or VictoriaLogs datasource and time range. |
| `suggest_loki_alloy_label_config` | `approvedLabels`*, `componentName`, `forwardTo`, `normalizeLogLevel`, `requiredLabels` | Generates an Alloy loki.process snippet enforcing an approved label set via stage.label_keep, with optional log-level normalisation and… |

## Tempo (Traces) (9)

| Tool | Parameters | Notes |
| --- | --- | --- |
| `find_slow_requests` | `labels`*, `name`*, `end`, `start` | Searches relevant Tempo datasources for slow requests, waits for the analysis to complete, and returns the results. |
| `tempo_docs-config` | `datasourceUid`*, `name`* | Documentation on Tempo configuration. Best for questions about how to configure or operate Tempo. |
| `tempo_docs-traceql` | `datasourceUid`*, `name`* | Documentation on TraceQL search. Best for retrieval of traces. |
| `tempo_get-attribute-names` | `datasourceUid`*, `scope` | Get a list of available attribute names that can be used in TraceQL queries. |
| `tempo_get-attribute-values` | `datasourceUid`*, `name`*, `filter-query` | Get a list of values for a fully scoped attribute name. |
| `tempo_get-trace` | `datasourceUid`*, `trace_id`* | Retrieve a specific trace by ID |
| `tempo_traceql-metrics-instant` | `datasourceUid`*, `query`*, `end`, `start` | Retrieve a single metric value given a TraceQL metrics query. |
| `tempo_traceql-metrics-range` | `datasourceUid`*, `query`*, `end`, `start` | Retrieve a metric series given a TraceQL metrics query. |
| `tempo_traceql-search` | `datasourceUid`*, `query`*, `end`, `start` | Search for traces using TraceQL queries |

## Pyroscope (Profiling) (4)

| Tool | Parameters | Notes |
| --- | --- | --- |
| `list_pyroscope_label_names` | `data_source_uid`*, `end_rfc_3339`, `matchers`, `start_rfc_3339` | Lists all available label names (keys) found in profiles within a specified Pyroscope datasource, time range, and optional label matchers. |
| `list_pyroscope_label_values` | `data_source_uid`*, `name`*, `end_rfc_3339`, `matchers`, `start_rfc_3339` | Lists all available label values for a particular label name found in profiles within a specified Pyroscope datasource, time range, and optional… |
| `list_pyroscope_profile_types` | `data_source_uid`*, `end_rfc_3339`, `start_rfc_3339` | Lists all available profile types available in a specified Pyroscope datasource and time range. |
| `query_pyroscope` | `data_source_uid`*, `profile_type`*, `end_rfc_3339`, `format`, `group_by`, `matchers`, `max_node_depth`, `query_type`, `start_rfc_3339`, `step` | Unified Pyroscope query tool for fetching profiles or metrics from Pyroscope. |

## Dashboards, Folders & Provisioning (11)

| Tool | Parameters | Notes |
| --- | --- | --- |
| `create_folder` | `title`*, `parentUid`, `uid` | Create a Grafana folder. Provide a title and optional UID. |
| `get_dashboard_by_uid` | `uid`* | Retrieves the complete dashboard, including panels, variables, and settings, for a specific dashboard identified by its UID. |
| `get_dashboard_panel_queries` | `uid`*, `panelId`, `variables` | Retrieve panel queries from a Grafana dashboard. |
| `get_dashboard_property` | `jsonPath`*, `uid`* | Get specific parts of a dashboard using JSONPath expressions to minimize context window usage. |
| `get_dashboard_summary` | `uid`* | Get a compact summary of a dashboard including title\, panel count\, panel types\, variables\, and other metadata without the full JSON. |
| `get_panel_image` | `dashboardUid`, `height`, `panelId`, `provisioningPreview`, `scale`, `theme`, `timeRange`, `timeout`, `variables`, `width` | Render a Grafana dashboard panel or full dashboard as a PNG image. |
| `list_provisioning_repositories` | `namespace` | List provisioning repositories (e.g. git-sync sources) configured for this Grafana instance. |
| `search_dashboards` | `limit`, `page`, `query` | Search for Grafana dashboards by a query string. |
| `search_folders` | `query` | Search for Grafana folders by a query string. |
| `update_dashboard` | `dashboard`, `folderUid`, `message`, `operations`, `overwrite`, `uid`, `userId` | Create or update a dashboard. Two modes: (1) Full JSON — provide 'dashboard' for new dashboards or complete replacements. |
| `validate_provisioning_file` | `path`*, `repo`*, `namespace`, `ref` | Validate a file in a provisioning repository at a given branch or commit by dry-run applying it. |

## Annotations (4)

| Tool | Parameters | Notes |
| --- | --- | --- |
| `create_annotation` | `dashboardUid`, `data`, `format`, `graphiteData`, `panelId`, `tags`, `text`, `time`, `timeEnd`, `what`, `when` | Create a new annotation on a dashboard or panel. |
| `get_annotation_tags` | `limit`, `tag` | Returns annotation tags with optional filtering by tag name. |
| `get_annotations` | `alertUid`, `dashboardUid`, `from`, `limit`, `matchAny`, `panelId`, `tags`, `to`, `type`, `userId` | Fetch Grafana annotations using filters such as dashboard UID, time range and tags. |
| `update_annotation` | `data`, `id`, `tags`, `text`, `time`, `timeEnd` | Updates the provided properties of an annotation by ID. |

## Alerting & Routing (4)

| Tool | Parameters | Notes |
| --- | --- | --- |
| `alerting_manage_routing` | `operation`*, `contact_point_title`, `datasource_uid`, `limit`, `name`, `time_interval_name` | Manage Grafana alerting routing configuration, including notification policies, contact points and time intervals. |
| `alerting_manage_rules` | `operation`*, `annotations`, `condition`, `data`, `datasource_uid`, `disable_provenance`, `exec_err_state`, `folder_uid`, `for`, `is_paused`, `keep_firing_for`, `label_selectors`, `labels`, `limit_alerts`, `matchers`, `missing_series_evals_to_resolve`, `no_data_state`, `notification_settings`, `org_id`, `record`, `rule_group`, `rule_limit`, `rule_type`, `rule_uid`, `search_folder`, `search_rule_name`, `states`, `title` | Manage Grafana alert rules with full CRUD capabilities and filtering. |
| `get_alert_group` | `alertGroupId`* | Get a specific alert group from Grafana OnCall by its ID. |
| `list_alert_groups` | `id`, `integrationId`, `labels`, `name`, `page`, `routeId`, `startedAt`, `state`, `teamId` | List alert groups from Grafana OnCall with filtering options. |

## Incidents & OnCall (10)

| Tool | Parameters | Notes |
| --- | --- | --- |
| `add_activity_to_incident` | `body`*, `incidentId`*, `eventTime` | Add a note (userNote activity) to an existing incident's timeline using its ID. |
| `create_incident` | `roomPrefix`*, `severity`*, `title`*, `attachCaption`, `attachUrl`, `isDrill`, `labels`, `status` | Create a new Grafana incident. Requires title, severity, and room prefix. |
| `get_assertions` | `endTime`*, `startTime`*, `entityName`, `entityType`, `env`, `namespace`, `site` | Get assertion summary for a given entity with its type, name, env, site, namespace, and a time range |
| `get_current_oncall_users` | `scheduleId`* | Get the list of users currently on-call for a specific Grafana OnCall schedule ID. |
| `get_incident` | `id`* | Get a single incident by ID. Returns the full incident details including title, status, severity, labels, timestamps, and other metadata. |
| `get_oncall_shift` | `shiftId`* | Get detailed information for a specific Grafana OnCall shift using its ID. |
| `list_incidents` | `drill`, `limit`, `status` | List Grafana incidents. Allows filtering by status ('active', 'resolved') and optionally including drill incidents. |
| `list_oncall_schedules` | `page`, `scheduleId`, `teamId` | List Grafana OnCall schedules, optionally filtering by team ID. |
| `list_oncall_teams` | `page` | List teams configured in Grafana OnCall. Returns a list of team objects with their details. |
| `list_oncall_users` | `page`, `userId`, `username` | List users from Grafana OnCall. These are OnCall users (separate from Grafana users). |

## Sift Investigations (3)

| Tool | Parameters | Notes |
| --- | --- | --- |
| `get_sift_analysis` | `analysisId`*, `investigationId`* | Retrieves a specific analysis from an investigation by its UUID. |
| `get_sift_investigation` | `id`* | Retrieves an existing Sift investigation by its UUID. |
| `list_sift_investigations` | `limit` | Retrieves a list of Sift investigations with an optional limit. |

## Datasources & Plugins (9)

| Tool | Parameters | Notes |
| --- | --- | --- |
| `check_datasources_health` | `offset`, `type`, `uids` | Check datasource health. Filter by type or UIDs; omit both to check all. |
| `create_datasource` | `name`*, `type`*, `access`, `basicAuth`, `database`, `fields`, `isDefault`, `schemaReviewed`, `url`, `withCredentials` | Create a datasource. If type is ambiguous, call search_plugin_information first; install the plugin if needed. |
| `get_datasource` | `name`, `uid` | Retrieves detailed information about a specific datasource by UID or name. |
| `get_plugin` | `pluginId`* | Check whether a Grafana plugin is installed and retrieve its details (name, version, type, enabled status). |
| `grafana_api_request` | `endpoint`*, `body`, `headers`, `jq`, `method` | Make an authenticated HTTP request to the Grafana API. |
| `install_plugin` | `pluginId`*, `version` | Install a Grafana plugin by its plugin ID. |
| `list_datasources` | `limit`, `offset`, `type` | List all configured datasources in Grafana. |
| `search_plugin_information` | `query`* | Search the Grafana plugin catalog by keyword to discover available plugins before installing or getting plugin details on a specific instance. |
| `update_datasource` | `uid`*, `access`, `basicAuth`, `database`, `fields`, `isDefault`, `jsonData`, `name`, `schemaReviewed`, `url` | Update non-secret datasource fields by UID. |

## Snapshots & Links (5)

| Tool | Parameters | Notes |
| --- | --- | --- |
| `create_snapshot` | `dashboard`*, `deleteKey`, `expires`, `external`, `key`, `name` | Create a Grafana snapshot from a full dashboard payload. |
| `delete_snapshot` | `key`* | Delete a Grafana snapshot by snapshot key. |
| `generate_deeplink` | `resourceType`*, `dashboardUid`, `datasourceUid`, `panelId`, `provisioningPreview`, `queries`, `queryParams`, `shorten`, `timeRange` | Generate deeplink URLs for Grafana resources. |
| `get_snapshot` | `key`* | Get a Grafana snapshot by key, including snapshot metadata and dashboard payload. |
| `list_snapshots` | `limit`, `query` | List Grafana dashboard snapshots with optional query and result limit filters. |

## Method

Two independent enumerations, cross-checked, both on 2026-08-17:

1. **Direct**: `tools/list` via the `mcp` Python client (the same client stack
   ADK uses) against the local server — 73 tools; parameter columns above are
   generated verbatim from the returned JSON schemas.
2. **Through the agent**: the [`grafana_probe`](../agents/grafana_probe/) ADK
   agent (Gemini via Vertex AI) was asked to enumerate its tools. Its list
   matched the direct enumeration **73/73 — nothing missing, nothing
   invented**. The grouping above is the agent's.

The server auto-skipped its Grafana Assistant tool category at startup
(log: `"Not enabling tools" category=assistant`) — an expected consequence of
the stack, not an error. Tool categories can be trimmed with the server's
`-disable-<category>` flags; the production DEAD AIR agent will run with a
least-privilege subset rather than all 73.

## Relevance to DEAD AIR

- **Triage:** `query_prometheus`, `query_loki_logs`, `tempo_traceql-search`,
  `find_error_pattern_logs`, `find_slow_requests` — the metrics/logs/traces
  sweep when viewer QoE degrades.
- **Postmortem:** `create_annotation` / `update_annotation` — dashboard
  annotations after verified recovery.
- **Escalation:** `create_incident`, `add_activity_to_incident`,
  `get_current_oncall_users` — the human-gated remediation path.
- **Context:** `get_dashboard_summary`, `get_dashboard_panel_queries`,
  `generate_deeplink` — linking findings back to what operators see.

