"""
Check the documentation Gateway before you use it in the runtime.

Usage (from the folder that holds gateway_auth.py):
  pip install "mcp>=1.9,<2" boto3 httpx
  python check_gateway.py --url https://<id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp
Options: --region us-east-1   --query "aws_sns_topic maximum_message_size"

It signs the request the way the runtime does, lists the Gateway's tools, runs one search, and checks that every result
link is on an allowed documentation site. Your login needs bedrock-agentcore:InvokeGateway on the gateway.
Exit code 0 means the runtime will be able to use it.
"""

import argparse
import asyncio
import re
import sys
from urllib.parse import urlparse

import gateway_auth

ALLOWED = ("developer.hashicorp.com", "docs.aws.amazon.com", "registry.terraform.io")
HINTS = (
    ("403", "The Gateway refused the request. Check that your login may call bedrock-agentcore:InvokeGateway on this gateway, "
            "and that --region matches the Gateway's region."),
    ("401", "The Gateway wants a different kind of authorization. This check signs requests (IAM). A gateway that uses a JWT token needs GATEWAY_TOKEN instead."),
    ("Name or service not known", "The host name could not be found. Check the URL, including the gateway id."),
    ("getaddrinfo", "The host name could not be found. Check the URL, including the gateway id."),
    ("credentials", "No AWS credentials were found. Sign in the way you normally do, then run this again."),
)


def leaves(error):
    """Flatten the exception groups that the async client raises."""
    inner = getattr(error, "exceptions", None)
    if inner:
        for e in inner:
            yield from leaves(e)
    else:
        yield error


async def run(url, region, query, allow_local=False):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    gateway_auth.validate_gateway_url(url, allow_local=allow_local)
    async with streamablehttp_client(url, auth=gateway_auth.GatewaySigV4(region)) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"Gateway tools ({len(tools)}):")
            for t in tools:
                props = ", ".join((t.inputSchema or {}).get("properties", {}).keys())
                print(f"  {t.name}   inputs: {props}")
            search = next((t for t in tools if "search" in t.name.lower()), None)
            if search is None:
                print("\nNOT OK: the Gateway has no search tool. Add the Web Search target to the gateway.")
                return 1
            print(f"\nSearching with {search.name}: {query!r}")
            result = await session.call_tool(search.name, {"query": query})
            text = "\n".join(getattr(c, "text", "") for c in result.content)
            if getattr(result, "isError", False):
                print("The tool returned an error:\n" + text[:600])
                print("\nNOT OK. If this says access denied, the Gateway's own service role needs bedrock-agentcore:InvokeWebSearch.")
                return 1
            urls = sorted(set(re.findall(r"https?://[^\s\"'<>)\]]+", text)))
            print(f"{len(urls)} link(s) in the result:")
            outside = []
            for u in urls[:12]:
                host = (urlparse(u).hostname or "").lower()
                fine = host in ALLOWED
                print(f"  {'ok     ' if fine else 'OUTSIDE'} {u[:110]}")
                if not fine and not host.endswith(".amazonaws.com"):
                    outside.append(u)
            if not urls:
                print("\nNOT OK: the search returned no links. Try another --query. The domain filter hides results it cannot verify.")
                return 1
            if outside:
                print("\nNOT OK: results came from sites outside the allowed list. Check the target's included domains.")
                return 1
            print("\nOK: the Gateway answers signed requests and its search stays on the allowed documentation sites.")
            return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--query", default="aws_sns_topic maximum_message_size")
    ap.add_argument("--allow-local", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        return asyncio.run(run(args.url, args.region, args.query, args.allow_local))
    except ValueError as e:
        print(f"NOT OK: {e}")
        return 1
    except BaseException as e:  # the async client wraps errors in groups
        for err in leaves(e):
            msg = f"{type(err).__name__}: {err}"
            print("NOT OK:", msg[:400])
            for needle, hint in HINTS:
                if needle.lower() in msg.lower():
                    print("  ->", hint)
                    break
        return 1


if __name__ == "__main__":
    sys.exit(main())
