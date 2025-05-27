import os
import json
import gzip
import time
import asyncio
import logging
import aiohttp

# Configure logging level based on DEBUG_LOGGING environment variable
logger = logging.getLogger()
debug_env = os.getenv("DEBUG_LOGGING", "false")
logger.setLevel(logging.DEBUG if debug_env.lower() in ("true", "1", "yes") else logging.INFO)

# Required env variables
USER_KEY   = os.environ.get("USER_KEY")    # New Relic User API key
INGEST_KEY = os.environ.get("INGEST_KEY")  # New Relic Ingest API key
ACCOUNT_ID = os.environ.get("ACCOUNT_ID")  # New Relic Account ID that data will be posted to
EVENT_TYPE = os.environ.get("EVENT_TYPE")  # New Relic table that data will be posted to

# Validate that required env vars are provided
if not USER_KEY or not INGEST_KEY or not ACCOUNT_ID or not EVENT_TYPE:
    raise RuntimeError("Missing required environment variables: USER_KEY, INGEST_KEY, or ACCOUNT_ID")

# Endpoints and headers
GRAPH_API_URL = "https://api.newrelic.com/graphql"
NRDB_URL      = f"https://insights-collector.newrelic.com/v1/accounts/{ACCOUNT_ID}/events"
GRAPHQL_HEADERS = {
    "Content-Type": "application/json",
    "API-Key": USER_KEY
}
NRDB_HEADERS = {
    "Content-Type": "application/json",
    "X-Insert-Key": INGEST_KEY,
    "Content-Encoding": "gzip"
}

TIME_WINDOW_MINUTES = 10  # Look back period for uptime data (last 10 minutes)

async def get_all_hosts(session):
    """Query New Relic GraphQL API to retrieve all hosts (entities of type HOST)."""
    cursor = None
    all_hosts = []
    # GraphQL query template for entity search (pages through results with cursor)
    query_template = """
    {
      actor {
        entitySearch(query: "type='HOST'") {
          results%s {
            entities {
              guid
              name
              account {
                id
                name
              }
              reporting
            }
            nextCursor
          }
        }
      }
    }"""
    # Fetch pages of hosts until no nextCursor
    while True:
        cursor_clause = f'(cursor: "{cursor}")' if cursor else ""
        query = {"query": query_template % cursor_clause, "variables": {}}
        try:
            async with session.post(GRAPH_API_URL, json=query, headers=GRAPHQL_HEADERS) as resp:
                data = await resp.json()
        except Exception as e:
            logger.error(f"Error fetching host list: {e}")
            raise
        if data.get("errors"):
            # GraphQL error occurred
            msg = data["errors"][0].get("message", "Unknown error")
            logger.error(f"GraphQL host query error: {msg}")
            raise Exception(f"GraphQL error: {msg}")
        # Append this page of hosts
        results = data.get("data", {}).get("actor", {}).get("entitySearch", {}).get("results", {})
        entities = results.get("entities", [])
        all_hosts.extend(entities)
        cursor = results.get("nextCursor")
        if not cursor:
            break  # no more pages
    return all_hosts

def batch_hosts(hosts):
    """Batch hosts into chunks (max 480 hosts or 5 unique account IDs per batch) to meet query limits."""
    batches = []
    current_accounts = set()
    current_hosts = []
    for host in hosts:
        acct_id = str(host["account"]["id"])
        # If adding a new account would exceed 5 unique accounts in this batch, start a new batch
        if acct_id not in current_accounts and len(current_accounts) >= 5:
            batches.append({"accountIds": list(current_accounts), "hosts": current_hosts})
            current_accounts = set()
            current_hosts = []
        # Add host to current batch
        current_accounts.add(acct_id)
        current_hosts.append(host)
        # If batch size reaches 480 hosts, finalize this batch and start a new one
        if len(current_hosts) >= 480:
            batches.append({"accountIds": list(current_accounts), "hosts": current_hosts})
            current_accounts = set()
            current_hosts = []
    # Add the final batch if not empty
    if current_hosts:
        batches.append({"accountIds": list(current_accounts), "hosts": current_hosts})
    return batches

async def fetch_uptime_data(session, host_batch, start_ms, end_ms):
    """Fetch 10-minute uptime/downtime metrics for a batch of hosts using a NRQL query via GraphQL."""
    # Build NRQL query string for a batch of hosts
    host_guids = [h["guid"] for h in host_batch["hosts"]]
    host_list_str = ",".join(f"'{guid}'" for guid in host_guids)  # e.g. 'guid1','guid2',...
    nrql_query = (
        "SELECT filter(uniqueCount(timestamp), where one_min_event_count > 0) as 'uptime_minutes', "
        "uniqueCount(timestamp) as 'total_minutes', "
        "filter(uniqueCount(timestamp), where one_min_event_count < 1) as 'downtime_minutes' "
        f"FROM (FROM SystemSample SELECT count(*) as 'one_min_event_count' where entityGuid in ({host_list_str}) "
        "facet entityGuid as 'guid' TIMESERIES 60 seconds LIMIT MAX) "
        f"since {start_ms} until {end_ms} facet guid LIMIT MAX"
    )
    # Construct GraphQL query with the NRQL string and account IDs
    accounts_list = ",".join(str(a) for a in host_batch["accountIds"])
    gql_query = {
        "query": f"""{{
            actor {{
                uptime: nrql(accounts: [{accounts_list}], query: "{nrql_query}", timeout: 90) {{
                    results
                }}
            }}
        }}""",
        "variables": {}
    }
    # Fetch hosts - maximum 4 retries
    for attempt in range(1, 4):
        try:
            async with session.post(GRAPH_API_URL, json=gql_query, headers=GRAPHQL_HEADERS) as resp:
                data = await resp.json()
        except Exception as e:
            logger.error(f"Request error on uptime query (attempt {attempt}): {e}")
            if attempt < 3:
                await asyncio.sleep(1)  # brief delay before retry
                continue
            else:
                raise
        if data.get("errors"):
            # New Relic returned a GraphQL error (e.g. timeout or query issue)
            msg = data["errors"][0].get("message", "")
            logger.error(f"GraphQL query error (attempt {attempt}): {msg}")
            if attempt < 3:
                await asyncio.sleep(1)
                continue
            else:
                raise Exception(f"GraphQL query failed: {msg}")
        # Extract results array from response
        results = data.get("data", {}).get("actor", {}).get("uptime", {}).get("results", [])
        if results:
            # Map results by hostname for quick lookup
            results_by_guid = { (r.get("guid")): r for r in results }
            output = []
            for host in host_batch["hosts"]:
                guid = host["guid"]
                if guid in results_by_guid:
                    rec = results_by_guid[guid]
                    # Add account/hostname/eventType
                    rec["accountId"]   = host["account"]["id"] or "Unknown"
                    rec["accountName"] = host["account"]["name"] or "Unknown"
                    rec["hostname"]    = host["name"]  or "Unknown"
                    rec["eventType"]   = EVENT_TYPE
                    del rec["facet"]
                    output.append(rec)
                else:
                    # Host had no data in this period -> mark as 100% downtime
                    output.append({
                        "eventType": EVENT_TYPE,
                        "downtime_minutes": TIME_WINDOW_MINUTES,
                        "hostname": host["name"],
                        "guid": guid,
                        "total_minutes": TIME_WINDOW_MINUTES,
                        "uptime_minutes": 0,
                        "accountId": host["account"]["id"],
                        "accountName": host["account"]["name"]
                    })
            return output
        else:
            # No hosts in this batch reported data -> all are down for this period
            return [
                {
                    "eventType": EVENT_TYPE,
                    "downtime_minutes": TIME_WINDOW_MINUTES,
                    "hostname": host["name"],
                    "guid": host["guid"],
                    "total_minutes": TIME_WINDOW_MINUTES,
                    "uptime_minutes": 0,
                    "accountId": host["account"]["id"],
                    "accountName": host["account"]["name"]
                }
                for host in host_batch["hosts"]
            ]

async def write_to_nrdb(session, compressed_payload):
    """Upload compressed payload to New Relic"""
    try:
        async with session.post(NRDB_URL, data=compressed_payload, headers=NRDB_HEADERS) as resp:
            if resp.status == 200:
                return "complete"
            else:
                # Log error response from New Relic (status code and body)
                error_text = await resp.text()
                logger.error(f"Failed to post to NRDB (HTTP {resp.status}): {error_text}")
                return "failed"
    except Exception as e:
        logger.error(f"Exception during data ingest: {e}")
        return "failed"

async def main():
    """Main function to orchestrate fetching host data, computing uptime, and sending results."""
    # Calculate time window (last 10 minutes)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (TIME_WINDOW_MINUTES * 60 * 1000)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        # Get all hosts across all accounts
        hosts = await get_all_hosts(session)
        logger.info(f"Retrieved {len(hosts)} hosts")
        if not hosts:
            logger.info("No hosts found - ending execution")
            return "no_hosts"
        # Batch hosts to respect query limits
        batches = batch_hosts(hosts)
        logger.debug(f"Batched hosts into {len(batches)} query sets")
        # Fetch uptime data for each batch in parallel
        tasks = [fetch_uptime_data(session, batch, start_ms, end_ms) for batch in batches]
        all_results = []
        if tasks:
            batch_results = await asyncio.gather(*tasks)
            for res in batch_results:
                all_results.extend(res)
        logger.debug(f"Computed uptime/downtime for {len(all_results)} hosts")
        # Compress payload
        payload_json = json.dumps(all_results).encode("utf-8")
        compressed_payload = gzip.compress(payload_json)
        logger.debug(f"Compressed payload size: {len(compressed_payload)} bytes")
        # Send to New Relic
        result = await write_to_nrdb(session, compressed_payload)
        if result != "complete":
            logger.error("Failed to write data to New Relic")
        else:
            logger.info("Uptime data successfully sent to New Relic")
        return result

def lambda_handler(event, context):
    """AWS Lambda entry point."""
    return asyncio.run(main())