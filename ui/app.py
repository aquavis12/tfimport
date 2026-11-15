"""
Streamlit UI for the Terraform importer AgentCore runtime.

Discovers AWS resources that no IaC tool manages, imports them into Terraform,
and shows the generated code, plan verdicts, and downloadable files.

The runtime ARN is auto-discovered from your AWS account via the
bedrock-agentcore-control API, so you do not need to set it by hand.

Usage:
  pip install -r requirements.txt
  streamlit run app.py

Your AWS login needs bedrock-agentcore:InvokeAgentRuntime on the runtime,
and read access to the results S3 bucket to download files from the UI.
"""

import json
import os
import uuid

import boto3
import streamlit as st
from botocore.config import Config

# ================================================================ page config

st.set_page_config(page_title="Terraform Importer", page_icon="🏗️", layout="wide")

# The first import in a new runtime session downloads Terraform and the AWS
# provider, which can take several minutes.  Set a generous read timeout so
# boto3 does not give up before the runtime finishes.
_RUNTIME_CONFIG = Config(
    read_timeout=900,
    connect_timeout=30,
    retries={"max_attempts": 0},
)


# ================================================================ helpers


def _client(service, region):
    return boto3.client(service, region_name=region, config=_RUNTIME_CONFIG)


def _caller_identity():
    try:
        return boto3.client("sts", region_name="us-east-1").get_caller_identity()
    except Exception:
        return None


def _parse_s3_uri(uri):
    """s3://bucket/key  ->  (bucket, key)"""
    if not uri or not uri.startswith("s3://"):
        return None, None
    parts = uri[5:].split("/", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _read_response(response):
    """Extract and parse JSON from an invoke_agent_runtime response body."""
    body = response.get("response")
    if body is None:
        return None
    # StreamingBlob: read everything at once.
    try:
        raw = body.read()
    except AttributeError:
        # Not a StreamingBlob; try iterating over chunks.
        parts = []
        try:
            for chunk in body:
                parts.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
        except TypeError:
            return None
        text = "".join(parts)
    else:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw) if raw else ""

    text = text.strip()
    if not text:
        return None

    # Most responses are a single JSON blob.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Event-stream fallback: collect "data:" lines and parse the last one.
    lines = [ln[6:] for ln in text.splitlines() if ln.strip().startswith("data: ")]
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"_raw": text[:5000]}


def invoke_runtime(runtime_arn, region, payload):
    """Call the AgentCore runtime with a JSON payload; return the parsed result dict."""
    client = _client("bedrock-agentcore", region)
    session_id = f"tfimport-{uuid.uuid4().hex}"
    body = json.dumps(payload).encode("utf-8")
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=body,
    )
    return _read_response(response)


def _discover_runtimes(region):
    """List AgentCore runtimes in a region via the control-plane API.

    Returns a list of {"name", "arn", "status"} dicts, or [] on any error
    (old boto3, no permissions, unknown region, etc.).
    """
    try:
        control = boto3.client("bedrock-agentcore-control", region_name=region)
    except Exception:
        return []
    runtimes = []
    try:
        paginator = control.get_paginator("list_agent_runtimes")
        pages = paginator.paginate()
    except Exception:
        try:
            resp = control.list_agent_runtimes()
            pages = [resp]
        except Exception:
            return []
    for page in pages:
        for r in page.get("agentRuntimes", page.get("runtimes", [])):
            runtimes.append(
                {
                    "name": r.get("agentRuntimeName", r.get("name", "?")),
                    "arn": r.get("agentRuntimeArn", r.get("arn", "")),
                    "status": r.get("status", "?"),
                }
            )
    return runtimes


def _list_runs(bucket, region, prefix="results/"):
    """List result run folders (results/account/region/timestamp-id/), newest first."""
    client = _client("s3", region)
    runs = []
    paginator = client.get_paginator("list_objects_v2")
    # Drill down three levels with delimiter listing to avoid reading every key.
    for p1 in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for acct in p1.get("CommonPrefixes", []):
            for p2 in paginator.paginate(Bucket=bucket, Prefix=acct["Prefix"], Delimiter="/"):
                for reg in p2.get("CommonPrefixes", []):
                    for p3 in paginator.paginate(Bucket=bucket, Prefix=reg["Prefix"], Delimiter="/"):
                        for run in p3.get("CommonPrefixes", []):
                            runs.append(run["Prefix"].rstrip("/"))
    runs.sort(reverse=True)
    return runs


def _list_files(bucket, region, prefix):
    """List objects under a prefix."""
    client = _client("s3", region)
    files = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            files.append({"key": obj["Key"], "size": obj["Size"]})
    files.sort(key=lambda f: f["key"])
    return files


def _get_object(bucket, key, region):
    client = _client("s3", region)
    return client.get_object(Bucket=bucket, Key=key)["Body"].read()


# ============================================================ result rendering


def _show_result(result, region, bucket_name):
    """Render the full import result on the page."""
    status = result.get("status")
    if status == "rejected":
        st.error(f"**Rejected:** {result.get('detail', 'unknown reason')}")
        return
    if status == "failed":
        st.error(
            f"**Failed at {result.get('stage', '?')}:** "
            f"{result.get('detail', 'unknown error')}"
        )
        return
    if status != "ok":
        st.error(f"Unexpected status: {status}")
        st.json(result)
        return

    # --- header row
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Account", result.get("account", "-"))
    col2.metric("Region", result.get("region", "-"))
    col3.metric("Build", result.get("build", "-"))
    col4.metric("Skill", result.get("skill_source", "-"))

    st.divider()

    # --- discovery summary
    st.subheader("What was found")
    discovery = result.get("discovery", {})
    counts = discovery.get("counts")
    if counts:
        st.json(counts)

    excluded = discovery.get("excluded_sample") or []
    if excluded:
        with st.expander(f"Excluded resources (sample of {len(excluded)})"):
            for item in excluded[:20]:
                st.text(f"  {item.get('type', '?')}  {item.get('id', '?')}  ({item.get('reason', '?')})")

    warnings = discovery.get("warnings") or []
    if warnings:
        with st.expander(f"Warnings ({len(warnings)})"):
            for w in warnings:
                st.warning(w)

    errors = discovery.get("errors") or []
    if errors:
        with st.expander(f"Discovery errors ({len(errors)})"):
            for e in errors:
                st.error(str(e))

    # --- import summary
    summary = result.get("import_summary")
    if summary:
        col1, col2, col3 = st.columns(3)
        col1.metric("Resource types", summary.get("types", 0))
        col2.metric("Ready", summary.get("ready", 0))
        col3.metric("Needs review", summary.get("needs_review", 0))

    # --- per-type terraform code
    types = result.get("types") or []
    if types:
        st.subheader("Terraform code")
        for t in types:
            _show_type(t)

    # --- output / files
    output = result.get("output")
    if output:
        st.subheader("Files")
        _show_output(output, region, bucket_name)


def _show_type(t):
    """Render one resource type's result in an expander."""
    rtype = t.get("resource_type", "?")
    status = t.get("status", "?")
    n = len(t.get("ids", []))
    icon = "✅" if status == "ok" else "⚠️"
    label = f"{icon}  {rtype}  ({n} resource{'s' if n != 1 else ''})"

    with st.expander(label, expanded=status == "ok"):
        col1, col2, col3 = st.columns(3)
        col1.write(f"**Status:** `{status}`")
        col2.write(f"**Code source:** `{t.get('hcl_source', '-')}`")
        col3.write(f"**Plan:** `{t.get('plan_verdict', '-')}`")

        ids = t.get("ids") or []
        if ids:
            shown = ids[:10]
            st.caption("IDs: " + ", ".join(shown) + (" …" if len(ids) > 10 else ""))

        notes = t.get("notes") or []
        for note in notes:
            st.info(note)

        if t.get("stage"):
            st.error(f"Failed at **{t['stage']}**: {t.get('detail', '')}")

        hcl = t.get("hcl")
        if hcl:
            st.code(hcl, language="hcl")
        else:
            raw = t.get("raw_hcl")
            if raw:
                st.code(raw, language="hcl")
                st.caption("Raw generated code (not validated).  See the notes for the reason.")


def _show_output(output, region, bucket_name):
    """Render the output / files section with download buttons."""
    status = output.get("status")
    if status == "skipped":
        st.info(f"Output skipped: {output.get('detail', '')}")
        return
    if status == "failed":
        st.error(f"Output failed: {output.get('detail', '')}")
        return

    s3_uri = output.get("s3_uri", "")
    if s3_uri:
        st.write(f"**S3 location:** `{s3_uri}`")

    bucket, prefix = _parse_s3_uri(s3_uri)
    if not bucket and bucket_name:
        bucket = bucket_name
        prefix = "results/"
    if not bucket:
        st.caption("No S3 location to browse.  Set the results bucket in the sidebar.")
        return

    # Ensure prefix ends with "/" so we list the folder, not a key prefix.
    list_prefix = prefix if prefix.endswith("/") else prefix + "/"

    try:
        files = _list_files(bucket, region, list_prefix)
    except Exception as e:
        st.warning(
            f"Could not read the files with your login ({type(e).__name__}).  "
            "Download them from the S3 console instead."
        )
        return

    if not files:
        st.caption("No files found at this location.")
        return

    for f in files:
        key = f["key"]
        name = key.rsplit("/", 1)[-1]
        size_kb = f["size"] / 1024
        try:
            data = _get_object(bucket, key, region)
            st.download_button(
                f"⬇  {name}  ({size_kb:.1f} KB)",
                data=data,
                file_name=name,
                key=f"import_dl_{key}",
            )
        except Exception:
            st.text(f"{name}  ({size_kb:.1f} KB)  — download failed")


# ================================================================ UI: sidebar

st.sidebar.title("🏗️ Terraform Importer")

identity = _caller_identity()
if identity:
    st.sidebar.caption(f"Logged in as:\n`{identity['Arn']}`")
else:
    st.sidebar.warning("No AWS credentials found.  Sign in the way you normally do before using this page.")

runtime_region = st.sidebar.selectbox(
    "Runtime region",
    ["us-east-1", "us-west-2", "eu-west-1", "ap-south-1", "ap-southeast-1"],
    index=0,
    help="The region where your AgentCore runtimes live (usually us-east-1).",
)

# Auto-discover runtimes.  Re-fetch when the region changes or on explicit refresh.
_cache_key = f"runtimes_{runtime_region}"
if st.session_state.get(_cache_key) is None or st.sidebar.button("🔄 Refresh"):
    st.session_state[_cache_key] = _discover_runtimes(runtime_region)
runtimes = st.session_state.get(_cache_key) or []

# Runtime selection: dropdown if runtimes were found, manual text input otherwise.
runtime_arn = ""
if runtimes:
    labels = [f"{r['name']}  ({r['status']})" for r in runtimes]
    arns = [r["arn"] for r in runtimes]
    # Default to the first READY runtime; otherwise the first one.
    default_idx = next((i for i, r in enumerate(runtimes) if r["status"] == "READY"), 0)
    selected_label = st.sidebar.selectbox(
        "AgentCore Runtime",
        labels,
        index=default_idx,
        help="Auto-discovered from your AWS account.  Choose a READY runtime.",
    )
    runtime_arn = arns[labels.index(selected_label)]
    st.sidebar.caption(f"ARN: `{runtime_arn}`")
else:
    runtime_arn = st.sidebar.text_input(
        "AgentCore Runtime ARN",
        value=os.environ.get("AGENT_RUNTIME_ARN", ""),
        placeholder="arn:aws:bedrock-agentcore:us-east-1:<acct>:runtime/terraform_importer/...",
        help="No runtimes found in this region.  Enter the ARN manually.",
    )

results_bucket = st.sidebar.text_input(
    "Results S3 bucket",
    value=os.environ.get("OUTPUT_BUCKET", ""),
    help="The bucket where imported Terraform files are saved.  Set this to download files from the UI.",
)

# ================================================================== UI: tabs

tab_import, tab_results, tab_runtimes = st.tabs(["Import", "Results", "Runtimes"])


# ----------------------------------------------------------- tab: import

with tab_import:
    st.header("Find and import AWS resources into Terraform")

    col1, col2 = st.columns(2)
    with col1:
        scan_region = st.text_input(
            "Region to scan",
            value="us-west-2",
            help="The AWS region to discover resources in.",
        )
    with col2:
        include_global = st.checkbox(
            "Also scan IAM roles (global resources)",
            value=False,
            help="IAM roles are global; scanning them lists all roles in the account.",
        )

    with st.expander("Cross-account (optional)"):
        st.caption(
            "Use this to scan a different AWS account.  The runtime assumes a "
            "read-only role in that account (tf-importer-target) using the external ID."
        )
        cross_account = st.checkbox("Scan a different account")
        role_arn = ""
        external_id = ""
        if cross_account:
            role_arn = st.text_input(
                "Role ARN",
                placeholder="arn:aws:iam::<target-account>:role/tf-importer-target",
            )
            external_id = st.text_input("External ID")

    with st.expander("Terraform state locations (optional)"):
        st.caption(
            "S3 prefixes or local paths that hold *.tfstate files.  "
            "Resources found in a state file are excluded from import."
        )
        state_text = st.text_area(
            "State locations (one per line)",
            height=80,
            placeholder="s3://my-tf-state-bucket/env/",
        )
    state_locations = [line.strip() for line in state_text.strip().splitlines() if line.strip()]

    if st.button("🚀 Find and Import", type="primary", disabled=not runtime_arn):
        if not scan_region.strip():
            st.error("Enter a region to scan.")
        else:
            payload = {"region": scan_region.strip(), "include_global": include_global}
            if cross_account and role_arn:
                payload["role_arn"] = role_arn.strip()
                payload["external_id"] = external_id.strip()
            if state_locations:
                payload["state_locations"] = state_locations

            st.info(
                "Starting import.  The first run in a new session can take several "
                "minutes while the runtime downloads Terraform and the AWS provider."
            )
            with st.spinner("Running discovery and import…"):
                try:
                    result = invoke_runtime(runtime_arn, runtime_region, payload)
                except Exception as e:
                    result = None
                    st.error(f"Runtime call failed: {type(e).__name__}: {e}")

            if result:
                _show_result(result, runtime_region, results_bucket)


# ----------------------------------------------------------- tab: results

with tab_results:
    st.header("Browse import results in S3")

    bucket = results_bucket or st.text_input(
        "Results bucket name",
        key="results_bucket_input",
        placeholder="tfimport-results-...",
    )

    if not bucket:
        st.info("Enter a results bucket name (or set it in the sidebar).")
    else:
        try:
            runs = _list_runs(bucket, runtime_region)
        except Exception as e:
            runs = []
            st.error(f"Could not list results: {type(e).__name__}: {e}")

        if not runs:
            st.info("No result runs found in this bucket.  Run an import first.")
        else:
            selected = st.selectbox(
                "Select a run",
                runs,
                format_func=lambda x: x.removeprefix("results/"),
            )
            if selected:
                files = _list_files(bucket, runtime_region, selected + "/")
                if not files:
                    st.info("No files in this run folder.")
                else:
                    # Show a summary.json preview if present.
                    summary_key = next(
                        (f["key"] for f in files if f["key"].endswith("summary.json")), None
                    )
                    if summary_key:
                        try:
                            raw = _get_object(bucket, summary_key, runtime_region)
                            with st.expander("summary.json"):
                                st.json(json.loads(raw))
                        except Exception:
                            pass

                    st.markdown("**Files:**")
                    for f in files:
                        key = f["key"]
                        name = key.rsplit("/", 1)[-1]
                        size_kb = f["size"] / 1024
                        try:
                            data = _get_object(bucket, key, runtime_region)
                            st.download_button(
                                f"⬇  {name}  ({size_kb:.1f} KB)",
                                data=data,
                                file_name=name,
                                key=f"browse_dl_{key}",
                            )
                        except Exception:
                            st.text(f"{name}  ({size_kb:.1f} KB)  — download failed")


# ---------------------------------------------------------- tab: runtimes

with tab_runtimes:
    st.header("AgentCore Runtimes")

    st.caption(
        "Runtimes auto-discovered from the selected region.  Pick one in the "
        "sidebar to use it for imports, or change the region in the sidebar to "
        "look elsewhere."
    )

    if runtimes:
        for r in runtimes:
            icon = "🟢" if r["status"] == "READY" else "🔴"
            with st.container(border=True):
                st.write(f"{icon}  **{r['name']}**  —  {r['status']}")
                st.code(r["arn"], language="text")
    else:
        st.info(
            "No runtimes found in this region.  Make sure you are signed in to "
            "the account that has the runtimes, and that the region matches."
        )
        st.caption(
            "If your boto3 is too old to know about bedrock-agentcore-control, "
            "upgrade it: `pip install --upgrade boto3`.  You can also paste the "
            "runtime ARN into the sidebar manually."
        )
