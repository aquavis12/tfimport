"""
Calling an AgentCore Gateway from the runtime.

A Gateway with IAM authorization checks an AWS Signature Version 4 signature on every request (signing name
"bedrock-agentcore"), so the runtime signs each request of its MCP client with its own role. No token or secret is
needed: the role only needs bedrock-agentcore:InvokeGateway on the gateway.

The signing follows what AWS's mcp-proxy-for-aws package does (botocore's SigV4Auth over the request as httpx is about to
send it, without the `connection` header, which the server does not use when it recomputes the signature), in a few
lines and without that package's 36 extra dependencies. For a Gateway that uses a bearer token instead, pass the token.

Credentials are only ever sent to an AWS host: the URL must be https and end in amazonaws.com or api.aws.
"""

from urllib.parse import urlparse

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

SIGNING_NAME = "bedrock-agentcore"
ALLOWED_HOST_SUFFIXES = (".amazonaws.com", ".api.aws")


def validate_gateway_url(url, allow_local=False):
    """Return the URL if it is safe to send signed requests to, else raise ValueError."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if allow_local and parsed.scheme in ("http", "https") and host in ("127.0.0.1", "localhost"):
        return url
    if parsed.scheme != "https":
        raise ValueError("the gateway URL must use https")
    if not host.endswith(ALLOWED_HOST_SUFFIXES):
        raise ValueError("the gateway URL must be an AWS host (amazonaws.com or api.aws)")
    return url


class GatewaySigV4(httpx.Auth):
    """httpx auth that signs each request with SigV4 using the current credentials."""

    requires_request_body = True  # the body is part of the signature

    def __init__(self, region, service=SIGNING_NAME, session=None):
        self.region = region
        self.service = service
        self._session = session or boto3.Session()

    def auth_flow(self, request):
        credentials = self._session.get_credentials()
        if credentials is None:
            raise RuntimeError("no AWS credentials are available to sign the gateway request")
        frozen = credentials.get_frozen_credentials()  # a consistent key, secret, and token even if they refresh
        headers = {k: v for k, v in request.headers.items() if k.lower() != "connection"}
        aws_request = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        SigV4Auth(frozen, self.service, self.region).add_auth(aws_request)
        request.headers.update(dict(aws_request.headers))
        yield request


def gateway_transport(url, region, token=None, allow_local=False):
    """Return a callable that opens the MCP transport to the Gateway (for strands' MCPClient)."""
    from mcp.client.streamable_http import streamablehttp_client

    validate_gateway_url(url, allow_local=allow_local)
    if token:
        return lambda: streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"})
    return lambda: streamablehttp_client(url, auth=GatewaySigV4(region))
