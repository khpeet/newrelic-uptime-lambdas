# Host Uptime Lambdas 

Lambda functions that poll for host uptime and planned maintenance windows, and forwards that data to New Relic. 

**Uptime** is defined in this solution as _the amount of time that the agent is reporting data to New Relic._


## Pre-Requirements

* [AWS CLI installed/configured](https://medium.com/@jeffreyomoakah/installing-aws-cli-using-homebrew-a-simple-guide-486df9da3092)
* [AWS SAM CLI installed](https://formulae.brew.sh/formula/aws-sam-cli)
* Python 3.13 (only for local development)
* Docker (only for local development)
* Necessary IAM permissions (see below)

### IAM Minimal Permissions for SAM CLI Deployments

#### AWS CloudFormation
Allows creation, update, deletion, and management of stacks and change sets, including IAM resource acknowledgements:

- `cloudformation:CreateStack`
- `cloudformation:UpdateStack`
- `cloudformation:DeleteStack`
- `cloudformation:DescribeStacks`
- `cloudformation:ListStackResources`
- `cloudformation:CreateChangeSet`
- `cloudformation:ExecuteChangeSet`
- `cloudformation:DeleteChangeSet`
- `cloudformation:GetTemplate`
- `cloudformation:DescribeStackEvents`
- **Capability flags:** must allow `CAPABILITY_IAM` or `CAPABILITY_NAMED_IAM` when deploying templates that include IAM resources.

---

#### S3
Used by SAM to package and upload function code and artifacts:

- `s3:PutObject`
- `s3:GetObject`
- `s3:ListBucket`
- `s3:DeleteObject`
- *(Optional)* `s3:CreateBucket` if SAM needs to provision the deployment bucket for you

---

#### AWS Identity and Access Management (IAM)
Needed to create and manage execution roles, inline policies, and to allow Lambda to assume roles:

- **Role lifecycle:**  
  - `iam:CreateRole`  
  - `iam:DeleteRole`
- **Policy management:**  
  - `iam:AttachRolePolicy`  
  - `iam:DetachRolePolicy`  
  - `iam:PutRolePolicy`  
  - `iam:DeleteRolePolicy`
- **Role passing:**  
  - `iam:PassRole`

---

#### Lambda
Required to create, update, configure, and delete Lambda functions:

- `lambda:CreateFunction`
- `lambda:UpdateFunctionCode`
- `lambda:UpdateFunctionConfiguration`
- `lambda:DeleteFunction`
- `lambda:GetFunction`
- `lambda:GetFunctionConfiguration`
- `lambda:AddPermission`
- `lambda:RemovePermission`

---

#### EventBridge (CloudWatch Events)
Grants ability to schedule the Lambda and manage targets:

- `events:PutRule`
- `events:DeleteRule`
- `events:PutTargets`
- `events:RemoveTargets`
- `events:DescribeRule`
- `events:ListRules`

---

#### CloudWatch Logs
Allows Lambda functions to create log groups/streams and write logs:

- `logs:CreateLogGroup`
- `logs:CreateLogStream`
- `logs:PutLogEvents`


## Host Uptime Poller
This lambda runs every 10 minutes, polling the previous 10 minutes of the `SystemSample` eventType to determine how many minutes all hosts have been up (reporting data) or down (not reporting data). Those results are then formatted and send to New Relic.

### Assumptions:
* All hosts are configured to send telemetry 1 min or less. If hosts are modified beyond that polling interval (> 1 min), the script logic will need to be updated accordingly, since it evaluates 1 minute timeslices to determine if that minute contains data from the agent or not.

## Host Planned Downtime Poller
This lambda runs every minute, polling whether hosts have a `muted: true` tag attached or not. That data is then sent to New Relic in order to query the sum total minutes that a given host is under a maintenance window or planned downtime.

### Assumptions
* Automation in place that adds & removes a `muted: true` tag to hosts at the start & end of maintenance windows. New Relic's [tagging API](https://docs.newrelic.com/docs/apis/nerdgraph/examples/nerdgraph-tagging-api-tutorial/) can be used to achieve this.

## Configuration
Both lambdas have the following configuration options, expressed as function environment variables. These can be configured within each lambda function's `template.yaml`, or within AWS itself after the function is deployed.

* `USER_KEY` - [User key](https://docs.newrelic.com/docs/apis/intro-apis/new-relic-api-keys/#user-key) used to fetch data from New Relic.
* `INGEST_KEY` - [Ingest key](https://docs.newrelic.com/docs/apis/intro-apis/new-relic-api-keys/#license-key) used to ingest custom events into New Relic.
* `ACCOUNT_ID` - The account id to report data to.
* `EVENT_TYPE` - The table within New Relic that will hold data reported by the function to query.
* `DEBUG_LOGGING` - When `true`, enables debug logging.

## Installation
1. Clone repo
2. `cd` into one of the lambda sub-directories
3. Configure `template.yaml` with required env variables.
4. Run `sam build`
5. Run `sam deploy --guided` and follow the prompts.

## Querying & Dashboarding
To explore the data within New Relic, query the configured table name via the `EVENT_TYPE` env variable on each function. By default, `host-uptime-poller` is `hostUptime` and `host-planned-downtime-poller` is `hostMutedHeartbeat`. For example, `SELECT * FROM hostUptime` will show all data reported by the function.

The following queries can be used as starting points to dashboard the data reported:

* Uptime per host, excluding planned downtime (only `host-uptime-poller` deployed):

```
WITH (FROM NrComputeUsage SELECT (aggregationendtime() - earliest(timestamp))/1000/60) as total_time_window_min FROM hostUptime SELECT clamp_max((sum(uptime_minutes)/latest(total_time_window_min))*100, 100) or 100 as 'Uptime %' facet hostname, guid LIMIT MAX
```

* Uptime per host, including planned downtime (both applications deployed):

```
WITH (FROM NrComputeUsage SELECT (aggregationendtime() - earliest(timestamp))/1000/60) as total_time_window_min FROM hostUptime LEFT JOIN (FROM hostMutedHeartbeat SELECT filter(count(*), where isMuted is true) as 'planned_downtime_minutes' facet host, guid LIMIT MAX) ON guid SELECT clamp_max(((round(latest(total_time_window_min)) - sum(downtime_minutes))/(round(latest(total_time_window_min)) - latest(planned_downtime_minutes or 0)))*100, 100) or 100 as 'Uptime %' facet hostname, guid LIMIT MAX until now
```

## Local Development
1. Clone repo
2. `cd` into one of the lambda sub-directories
3. Configure `template.yaml` with required env variables.
4. Run `sam build`
5. Run `sam local invoke <name-of-resource-in-template-file>` (i.e - `sam local invoke HostUptimePoller`)

## Additional Notes

* Infrastructure host entities have a TTL of 8 days. If a maintenance window for a given host goes beyond that (meaning the host hasn’t reported data for a consecutive 8 days), this solution will not see those hosts anymore (as they are no longer entities in New Relic and completely gone). Therefore, downtime minutes will not be captured for those hosts. [Reference](https://github.com/newrelic/entity-definitions/blob/main/entity-types/infra-host/definition.yml#L318C2-L318C35)
* Retries are built in to api requests performed in each lambda (4 max) for more resiliency.