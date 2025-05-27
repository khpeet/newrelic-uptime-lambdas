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
              tags {
                key
                values
              }
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

@staticmethod
def pluckTagValue(tags, key_to_pluck):
    """ Pluck and return a tag value from a given tags array based on key"""
    result = next((t for t in tags if t['key'] == key_to_pluck), None)
    return result['values'][0] if result else None

def process_hosts(hosts):
    """Determine if `muted: true` tag exists across hosts"""
    parsed_hosts = []
    for host in hosts:
        is_under_maintenance = False
        muted = pluckTagValue(host["tags"], "muted")
        if muted == "true":
            is_under_maintenance = True
        parsed_hosts.append({
            "eventType": EVENT_TYPE,
            "host": host["name"],
            "guid": host["guid"],
            "isMuted": is_under_maintenance,
            "accountId": host["account"]["id"],
            "accountName": host["account"]["name"]
        })
    
    return parsed_hosts

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
    """Main function to orchestrate fetching host data, determine if muted tag exists, and sending results."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        # Get all hosts across all accounts
        hosts = await get_all_hosts(session)
        logger.info(f"Retrieved {len(hosts)} hosts")
        if not hosts:
            logger.info("No hosts found - ending execution")
            return "no_hosts"
        
        # Process hosts
        all_results = process_hosts(hosts)

        # Compress payload
        payload_json = json.dumps(all_results).encode("utf-8")
        compressed_payload = gzip.compress(payload_json)
        logger.debug(f"Compressed payload size: {len(compressed_payload)} bytes")

        # Send to New Relic
        result = await write_to_nrdb(session, compressed_payload)
        if result != "complete":
            logger.error("Failed to write data to New Relic")
        else:
            logger.info("Planned downtime data successfully sent to New Relic")
        return result

def lambda_handler(event, context):
    """AWS Lambda entry point."""
    return asyncio.run(main())