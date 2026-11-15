# Deploy the importer (importer account only)

Goal: get the AgentCore Runtime running in account `111111111111`, us-east-1, and prove it can find and import AWS resources that no IaC manages. Cross-account comes later.

In this mode the runtime uses its own role to read the account. You skip `role_arn`, `external_id`, and the CloudFormation template.

## The short version

1. Upload `deployment_package.zip` to S3 at `importer/deployment_package.zip`, and create the results bucket (Step 5).
2. Create the runtime role from `iam/permissions-policy.json` + `ReadOnlyAccess` (Step 6).
3. Create the runtime with `OUTPUT_BUCKET`, `SKILL_REGISTRY_ID`, and `GATEWAY_URL` set (Step 7).
4. Register the import skill in Agent Registry and set `SKILL_REGISTRY_ID` on the runtime (Step 10).
5. Set up the AgentCore Gateway and set `GATEWAY_URL` on the runtime (Step 11).
6. Create two test resources, then run the Streamlit app against them (Steps 8 and 9). You get Terraform files, a README, and a zip in S3, ready to download.

## What is in this repo

| File | What it is |
|---|---|
| `deployment_package.zip` | The ready-made upload for AgentCore. Nothing to build. |
| `iam/permissions-policy.json` | The runtime role's inline policy. |
| `iam/skill-definition.json` | The skill definition JSON for Agent Registry. |
| `ui/app.py`, `ui/requirements.txt` | The Streamlit web UI. |
| `docs/SKILL.md` | The import skill (naming, tags, safety, gates). |
| `docs/DEPLOY.md` | This file. |
| `docs/images/tfimport.png` | Architecture diagram. |
| `main.py`, `gateway_auth.py`, `check_gateway.py` | Runtime source and gateway utilities. |

You need an AWS login for account `111111111111` and Python 3 (for the Streamlit app and the check scripts). The AWS CLI is only needed for the CLI options below, and Terraform only for the role-by-Terraform option. The runtime brings its own Terraform.

---

## Step 1. Check your login

```powershell
aws sts get-caller-identity
python --version
```

Expected: account `111111111111`, and a Python 3 version. If the account is different, log in the way you normally do (SSO or your usual credentials) before going on. Every command below uses that login, so there is no profile to set up.

## Step 2. Set variables

```powershell
$Region      = "us-east-1"
$CodeBucket  = "tfimport-agentcore-code-111111111111-us-east-1"
$ResultsBucket = "tfimport-results-111111111111-us-east-1"   # where the Terraform files and README are saved
$TestRegion  = "us-west-2"     # a region you do not use, so the test finds only what you create
```

## Step 3. Local check (no AWS calls, optional)

Keep `check_local.py`, `main.py`, `discovery.py`, `importer.py`, `docs/SKILL.md`, and `build_zip.py` in one folder, then:

```powershell
python check_local.py
```

Expected: `syntax ok` for four files, `found` for `SKILL.md`, `accepted` for two good IDs, `rejected` for four bad ones, and `ALL LOCAL CHECKS PASSED`. (`main.tf` is optional and only matters for the Terraform role option in Step 6.)

## Step 4. Get the zip

### Option A: use the ready-made zip (no build)

`deployment_package.zip` is 39 MB. SHA-256: `c9f01388ce529a74b51fd3ff615ca727407717d3bd580391e9404eefc458b753`. Build ID: `a9fc2f8dce` (the runtime log and every result show the build that produced them). Check it after downloading if you like:

```powershell
Get-FileHash deployment_package.zip -Algorithm SHA256
```

What is inside:
- The code, the `import-tf` skill, and the Python dependencies built for arm64 and Python 3.13.
- Precompiled Python bytecode, and the heavy packages load on the first request, so the server starts in about a second. AgentCore allows a runtime only 30 seconds to start.
- **Not** Terraform or the AWS provider. The runtime downloads Terraform from HashiCorp on its first import (and checks the download against HashiCorp's SHA-256 list), and `terraform init` downloads the AWS provider from the Terraform registry. The runtime also downloads `terraform-docs` from GitHub when it writes the README (checked against the published checksum). All of that needs outbound internet, which Public network mode has. The first import in a new session takes a minute or two longer. Later imports in the same session reuse the downloads.

### Option B: build it yourself

```powershell
python build_zip.py --slim     # same as Option A. Run it with Python 3.13 to get the precompiled bytecode.
python build_zip.py            # full build: also bundles Terraform and the AWS provider (about 730 MB unzipped, close to the 750 MB limit)
```

The full build needs internet access to HashiCorp while it builds, and then works without outbound internet at run time. The zip lands in `%TEMP%\tfimport-build\dist\`.

## Step 5. Upload the zip

**Console:** S3, open (or create) the bucket `tfimport-agentcore-code-111111111111-us-east-1` in us-east-1, **Create folder** named `importer`, open it, and **Upload** the `deployment_package.zip` you downloaded (or the one from your own build). The key must end up exactly `importer/deployment_package.zip`, because the runtime role is only allowed to read that path.

**CLI:**

```powershell
aws s3api head-bucket --bucket $CodeBucket --region $Region
```

If that errors with "Not Found", create it:

```powershell
aws s3api create-bucket --bucket $CodeBucket --region $Region
aws s3api put-public-access-block --bucket $CodeBucket --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Upload (run it from the folder that holds the zip):

```powershell
aws s3 cp .\deployment_package.zip "s3://$CodeBucket/importer/deployment_package.zip" --region $Region
aws s3api head-object --bucket $CodeBucket --key importer/deployment_package.zip --query "[ContentLength,VersionId]"
```

Expected size: about 39 MB for the ready-made zip (about 170 MB for the full build).

### Create the results bucket

Each run saves its Terraform files, README, and a zip here, so people can download them. Use a separate private bucket: the files describe your resources.

**Console:** S3, **Create bucket**, name `tfimport-results-111111111111-us-east-1`, region us-east-1, leave **Block all public access** on, and create it. New buckets are encrypted with S3-managed keys by default. Keep it that way: the runtime asks for that encryption on every upload, and it has no permission to use a KMS key.

**CLI:**

```powershell
aws s3api create-bucket --bucket $ResultsBucket --region $Region
aws s3api put-public-access-block --bucket $ResultsBucket --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Optional: add a lifecycle rule that deletes objects under `results/` after 30 days, so old runs do not pile up.

## Step 6. Create the runtime role

### Option A: in the console (recommended)

1. IAM, Roles, **Create role**.
2. Trusted entity type: **Custom trust policy**. Paste the contents of `iam/trust-policy.json`. (The AgentCore documentation uses this same principal, `bedrock-agentcore.amazonaws.com`, with a condition on your account and region.)
3. Attach the AWS managed policy **ReadOnlyAccess**. Terraform's plan refreshes every attribute, so it needs broad read access. The inline policy below blocks the sensitive reads.
4. Name the role `importer-runtime-role` and create it.
5. Open the role, **Add permissions**, **Create inline policy**, JSON tab, and paste `iam/permissions-policy.json`. Name it `importer-runtime-read`.
6. Copy the role ARN, then:
   ```powershell
   $RuntimeRoleArn = "arn:aws:iam::111111111111:role/importer-runtime-role"
   ```

**If you already made the role from an earlier version of these files:** open it again, **Trust relationships**, **Edit trust policy**, and paste the new `iam/trust-policy.json`. Then edit the inline policy `importer-runtime-read` and paste the new `iam/permissions-policy.json`. The new files add the logging, tracing, and metrics permissions that the AgentCore documentation lists for a direct-deployment execution role, lock the trust to your account and region, add read access to Agent Registry (used by the optional skill step), and add permission to write results to the results bucket (the `WriteResults` statement: `s3:PutObject` on `tfimport-results-111111111111-us-east-1/results/*` only).

The permissions policy has the code bucket name `tfimport-agentcore-code-111111111111-us-east-1` in its first statement and the results bucket name in the `WriteResults` statement. If you use different buckets, change both.

### Option B: Terraform

`main.tf` predates the AgentCore logging, tracing, and registry statements in `iam/permissions-policy.json`. Prefer Option A, or add those statements to `main.tf` first.

```powershell
terraform init
terraform apply
$RuntimeRoleArn = (terraform output -raw runtime_role_arn)
$RuntimeRoleArn
```

Type `yes` when asked.

**Check:** `$RuntimeRoleArn` ends in `role/importer-runtime-role`. Wait about 30 seconds before the next step, because a new role takes a moment to become usable.

## Step 7. Create the AgentCore Runtime

### Option A: in the console (recommended)

Bedrock AgentCore, **Runtime**, **Create**, **Direct code deployment**. Use these values:

| Field | Value |
|---|---|
| Name | `terraform_importer` (letters, digits, underscores only) |
| Code location | bucket `tfimport-agentcore-code-111111111111-us-east-1`, key `importer/deployment_package.zip` |
| Runtime | Python 3.13 |
| Architecture | arm64 (the zip holds arm64 binaries) |
| Entry point | `main.py` |
| Execution role | existing role `importer-runtime-role` |
| Network | Public |
| Environment variable | `MODEL_ID` = `nvidia.nemotron-super-3-120b` |
| Environment variable | `OUTPUT_BUCKET` = `tfimport-results-<account-id>-us-east-1` (without it the run still works but saves nothing) |
| Environment variable | `SKILL_REGISTRY_ID` = your Agent Registry ID — the LLM's import skill is loaded from here; falls back to the bundled `docs/SKILL.md` if unreachable (see Step 10) |
| Environment variable | `GATEWAY_URL` = your AgentCore Gateway URL — the LLM calls this to search Terraform and AWS docs while tidying code (see Step 11). Leave `GATEWAY_TOKEN` unset: the runtime signs its requests. |

Keep the name `terraform_importer`: the role's log permissions are written for it. The console screens may label things a little differently; the values are what matter. Create it, wait for status **Ready**, then copy the runtime ARN and ID from its details page:

```powershell
$RuntimeArn = "<paste the runtime ARN>"
$RuntimeId  = "<paste the runtime ID>"
```

If it ends in a failed state, open the runtime's details and read the failure reason.

**After every new upload, update the runtime.** AgentCore takes the zip when you create or update the runtime (I have not confirmed whether it also reads S3 live), so putting a new file in S3 may change nothing by itself. In the console, open the runtime, choose **Edit**, check the code location, and save. Wait for **Ready**, then start a new session, because existing sessions keep the code they started with.

**Check which code is running.** The runtime log shows the build at startup: `main.py: build <id>; imports done in ...s; starting server on 0.0.0.0:8080`. Every result carries the same `build` value, and the Streamlit page shows it under the account line. Compare it with the Build ID in Step 4. If they differ, the runtime is still on an older zip.

**Tip: give every build its own file name.** Upload the zip as `importer/deployment_package-<build id>.zip` and point the runtime at that name. A new file name leaves no doubt that the runtime takes the new file. Any name under `importer/` works with the role's policy.

### Option B: CLI

```powershell
@"
{
  "agentRuntimeName": "terraform_importer",
  "description": "Imports AWS resources not managed by Terraform",
  "agentRuntimeArtifact": {
    "codeConfiguration": {
      "code": { "s3": { "bucket": "$CodeBucket", "prefix": "importer/deployment_package.zip" } },
      "runtime": "PYTHON_3_13",
      "entryPoint": ["main.py"]
    }
  },
  "roleArn": "$RuntimeRoleArn",
  "networkConfiguration": { "networkMode": "PUBLIC" },
  "environmentVariables": { "MODEL_ID": "nvidia.nemotron-super-3-120b", "OUTPUT_BUCKET": "$ResultsBucket" }
}
"@ | Out-File -Encoding ascii "$env:TEMP\create-runtime.json"

aws bedrock-agentcore-control create-agent-runtime --region $Region `
  --cli-input-json "file://$env:TEMP\create-runtime.json"
```

Save `agentRuntimeId` and `agentRuntimeArn` from the response (`$RuntimeId`, `$RuntimeArn` as above), then wait for `READY`:

```powershell
aws bedrock-agentcore-control get-agent-runtime --region $Region `
  --agent-runtime-id $RuntimeId --query "[status,failureReason]"
```

If the CLI rejects the request, use the console option.

## Step 8. Create two test resources (no IaC)

```powershell
$p = @("--region",$TestRegion)
aws sqs create-queue @p --queue-name handmade-queue
aws sns create-topic @p --name handmade-topic
```

Both are free and sit in a region you don't use, so discovery should find exactly these.

## Step 9. Run it

### Option A: the Streamlit app (recommended)

```powershell
cd ui
pip install -r requirements.txt
streamlit run app.py
```

Your login must be allowed to call `bedrock-agentcore:InvokeAgentRuntime` (an admin login is). The app auto-discovers your runtimes — select `terraform_importer` from the sidebar dropdown. In the page, choose:
- **Region to scan:** `us-west-2`
- **Also scan IAM roles:** off

Then click **Find and Import**.

### Option B: CLI

```powershell
@"
{
  "region": "$TestRegion",
  "include_global": false
}
"@ | Out-File -Encoding ascii "$env:TEMP\payload.json"

$SessionId = "tfimport-" + [guid]::NewGuid().ToString("N")
aws bedrock-agentcore invoke-agent-runtime --region $Region `
  --agent-runtime-arn $RuntimeArn --runtime-session-id $SessionId `
  --content-type application/json --payload "fileb://$env:TEMP\payload.json" `
  --cli-read-timeout 0 "$env:TEMP\result.json"
Get-Content "$env:TEMP\result.json"
```

### A passing run

In the Streamlit app:
- Under **What was found**, the numbers add up: the unmanaged resources plus the groups that were left out.
- Under **Terraform code**, your two test resources (`aws_sqs_queue` and `aws_sns_topic`) each show **ready**: terraform plan only imports them and changes nothing.
- Under **Files**, you see an `s3://` location, a zip you can download, and the README.

Other unmanaged resources in that region show up too (for example a repository that Elastic Beanstalk created). That is expected: the tool lists everything in the region that no IaC manages. The default VPC and its parts are left out on purpose.

From the CLI, the same facts are in the JSON: `status` is `ok`, each entry in `types` has `status: "ok"` and `plan_verdict: "no_changes"`, and `output.status` is `ok`.

A warning that no `state_locations` were given is expected. It only means anything managed by Terraform would show as unmanaged, which doesn't matter in a region with no Terraform.

### What gets saved to S3

Each run writes a new folder in the results bucket: `results/<account>/<region>/<time>-<id>/`.

| File | What it is |
|---|---|
| `providers.tf` | Terraform and AWS provider settings, so `terraform init` works in the folder |
| `<resource type>.tf` | The checked code for one resource type: the resource blocks plus the import blocks |
| `needs-review/<type>.tf.txt` | Code Terraform could not validate on its own, with the error as a comment (`.txt`, so Terraform ignores it) |
| `README.md` | What was found, how to use the files, and the tables that terraform-docs generates |
| `summary.json` | The same facts as JSON |
| `terraform-import-<...>.zip` | All of the above in one download |

People download the zip from the Streamlit page or from the S3 console. To use the files: unzip, run `terraform init`, `terraform plan`, then `terraform apply`. The README explains each step.

How the code is produced: Terraform writes it (`generate-config-out`). If the provider rejects placeholder values that Terraform wrote for arguments that are not set in AWS (for example `maximum_message_size = 0` on an SNS topic), the importer removes just those lines in code and checks again. The agent then tidies the code, or repairs it if Terraform still rejected it. The agent's version is used only if Terraform agrees with it: it contains only resource and import blocks for exactly the discovered IDs, and `fmt`, `validate`, and a plan that only imports all pass. Otherwise the code Terraform wrote is used, and the notes say why. A type that cannot be turned into valid code is never dropped: it appears with its stage and error, and its code goes to `needs-review/`.

### Negative tests (each must return `status: rejected`)
- Change `region` to `us-east-1; rm -rf /`.
- Add `"role_arn": "arn:aws:iam::123456789012:role/x"` with no `external_id`.
- Add `"state_locations": ["http://x"]`.

## Step 10. If something fails

Read the runtime log first. It is the fastest way to see what happened:

```powershell
aws logs tail "/aws/bedrock-agentcore/runtimes/$RuntimeId-DEFAULT" --region $Region --since 30m
```

A healthy run logs one line per step. For each resource type: `import <type> x<count>: status=... plan=...` (with the stage and Terraform's error if it failed), then `type <type>: status=... code=... plan=...`, and at the end `publish: status=ok s3://...`.

| Symptom | Likely cause |
|---|---|
| `ImportError: cannot import name 'eval_type_backport' from 'pydantic._internal._typing_extra'` | An older `mcp` is installed next to a newer `pydantic`. I reproduced this error with `mcp` 1.9.4, 1.12.4, and 1.20.0 against `pydantic` 2.14.3; `mcp` 1.25.0 and 1.30.0 work. The ready-made zip has `mcp` 1.30.0 and `pydantic` 2.14.3, which work together, so this error means the runtime is still running an older build: update the runtime to the new zip (Step 7). If you build the zip yourself, use the `build_zip.py` from this folder, which pins both versions. |
| `Runtime initialization time exceeded ... 30s` | The server did not come up in time, or crashed while starting. A crash while importing also shows up as this timeout, because the server never starts. A healthy start prints `main.py: imports done in ...s; starting server on 0.0.0.0:8080`. A Python traceback instead means an import failed: send me its first lines. |
| `This runtime is not MMDSv2-enabled` | An older runtime needs the setting turned on. `UpdateAgentRuntime` takes `metadataConfiguration` with `requireMMDSV2` set to true, and it also requires the artifact and role again. Repeat your network and environment settings too, so nothing is reset (I could not confirm how omitted fields are treated). Creating a new runtime avoids this. |
| You uploaded a new zip but nothing changed | Existing sessions keep the code they started with. Use a new session ID (the Streamlit app makes a new one on every run; for the CLI, generate a new `$SessionId`). |
| Runtime `CREATE_FAILED` | Read `failureReason`. Usually an incomplete zip, or the role not ready yet. Wait and retry. |
| "artifact contains binary files that are incompatible with Linux ARM64" | The zip was built on the wrong platform. Use the ready-made zip, or rebuild with `build_zip.py`, which downloads arm64 packages. |
| `ImportError` or `ModuleNotFoundError` in the log on the first request | The zip was built with the wrong Python version or architecture. Use the ready-made zip. |
| The log says `gateway unavailable (... HTTP 403)` | The Gateway refused the signed request. Check that the runtime role has the `InvokeDocsGateway` statement for this gateway's ARN, and that `GATEWAY_URL` is the URL of that same gateway. The run itself is not affected. |
| The log says `gateway unavailable (ValueError ... AWS host)` | `GATEWAY_URL` is not an https AWS address. The runtime refuses to send signed requests anywhere else. |
| The log says `gateway: 1 tool(s)` but a note never says it searched | The agent searches only when it needs to know what an argument means. A clean import needs no search. |
| `stage: discovery` | An AWS call failed before any import: the detail shows the error, for example AccessDenied on a describe call or on assume-role. The runtime role needs the read permission it names. |
| `stage: terraform_download` | The runtime could not download Terraform. Check the network mode is Public. In a restricted network, use the full build (Step 4, Option B). |
| `stage: init` and the detail mentions the registry or a timeout | The provider download failed. Retry once. If it keeps failing, the runtime has no outbound access to registry.terraform.io. Use the full build. |
| `stage: init` and the detail mentions the provider mirror | Full build only: check `providers/registry.terraform.io/hashicorp/aws/6.71.0/linux_arm64/` is in the zip. |
| `exec format error` | Full build only: the Terraform binary is not arm64. Rebuild. |
| A note says `the model did not return JSON` or `the model returned no code` | The model's answer could not be used. The code Terraform generated is used instead, so nothing is lost. Retry once, or try another `MODEL_ID`. |
| `stage: generate-config-out` with an error such as `expected max_message_size to be in the range ...` | The AWS provider is older than the resource. It was fixed by moving to provider 6.71.0 (the SQS limit changed in provider 6.8.0). If you still see an error like this for another resource, send it to me. The code Terraform wrote and its error are kept, and the agent tries to repair it. |
| A note says `removed ... (Terraform wrote an unset value that the AWS provider rejects)` | Terraform writes `0`, `null`, or an empty value for arguments that are not set in AWS, and for some resource types the provider rejects those placeholders (SNS topics are one). The importer removes only the lines the provider's own error names, and only when the value is a placeholder, which leaves the argument unset (the provider default). The plan then runs again, and if it still shows differences it says `has_changes`. |
| `stage: validate` on a type | Terraform's own generated code did not validate (a known quirk for some resource types, for example two arguments that conflict). The agent tries to repair it. If it cannot, the Terraform error is in the result and the README, and the code is in `needs-review/`. |
| `plan_verdict: has_changes` | The code differs from the real resource. The README shows the plan output. Review the differences before you apply. |
| A note says `the agent's version was not used (...)` | The agent's code failed one of the checks, and the reason is in the brackets. The code Terraform generated is used. This is safe. |
| `output.status: failed` with `AccessDenied` | The runtime role cannot write to the results bucket. Check the `WriteResults` statement (bucket name and the `results/` prefix) and that `OUTPUT_BUCKET` is the same bucket. |
| `output.status: skipped` | `OUTPUT_BUCKET` is not set on the runtime. Add it, update the runtime, and run again. |
| The app says it "could not read them with your login" | The files are in S3, but your login cannot read the bucket. Download them from the S3 console, or sign in with a login that can read it. |
| The log says `registry lookup failed (... ResourceNotFoundException ...)` | The runtime asked for a registry that does not exist. The log prints the ID it used between quotes. The most common cause is using a **Record ID** (from a record's page) instead of the **Registry ID** (from the registry's own page). Otherwise compare it letter by letter with the registry page, because a capital I and a lowercase l look alike. |
| An entry under `discovery.errors` | The role lacks read access for that one service. The other services still ran. |
| `skill_source` is `bundled` when you expected the registry | See the registry section below. The runtime log has a line starting with `skill:` that says why. |

## Step 11. Clean up the test resources

```powershell
aws sqs delete-queue @p --queue-url (aws sqs get-queue-url @p --queue-name handmade-queue --query QueueUrl --output text)
aws sns delete-topic @p --topic-arn "arn:aws:sns:${TestRegion}:111111111111:handmade-topic"
```

---

## Step 10. Register the import skill in Agent Registry

The LLM's system prompt is the import skill — it defines naming conventions, tag handling, safety rules, and the `fmt`/`validate`/`plan` gates every output must pass. The runtime always loads it. By registering it in Agent Registry you can update the skill without rebuilding the zip. If the registry is unreachable for any reason, the run falls back to the copy bundled in the zip and keeps working.

AWS Agent Registry uses the `agent-registry` namespace: its own console, IAM prefix (`agent-registry:`), and ARNs (`arn:aws:agent-registry:...`). The older `bedrock-agentcore` registry APIs were retired on 30 October 2026.

Do this after Step 9 has passed once.

1. **Get your Registry ID.** Open the Agent Registry console, find your registry (e.g. `tfimport-skills`). The page that shows its name and status has a **Registry ID** — copy it. A record's page has a different **Record ID**; the runtime needs the Registry ID, not the Record ID.
2. **Create the skill record.** In the registry, create a record of type *Skill* and **fill in every field**:
   - **Name:** `import-tf`
   - **Description:** `Clean Terraform generated from existing AWS resources. Naming, module layout, tags, safety, and the fmt, validate, and plan gates.`
   - **Version:** `1.0`
   - **Included content:** skill documentation and definition
   - **Skill definition (JSON):** the contents of `iam/skill-definition.json`
   - **Skill markdown:** the whole `docs/SKILL.md`
   - Click **Create and submit for approval**. With auto-approval on, it publishes immediately.

   If you leave **Name** empty the console generates a name like `record_mkvh0` and the agent will not find the record (it searches for the exact name `import-tf`). If that happens, open the record, choose **Edit**, and fix the name, description, and version.

3. **Role permission.** `iam/permissions-policy.json` has the `ReadSkillFromRegistry` statement with the `agent-registry:` actions. Make sure the runtime role has it (Step 6).
4. **Set the runtime environment variable.** Edit the runtime and add `SKILL_REGISTRY_ID` = the Registry ID you copied. Optional: `SKILL_NAME` (default `import-tf`) and `SKILL_REGISTRY_REGION` (default `us-east-1`).
5. **Verify.** Run again in a new session. The result should contain `"skill_source": "registry:1.0"` and the Streamlit page shows it under the account line. If it says `bundled`, the runtime log has a line starting with `skill:` that says whether no approved record was found or the lookup failed.

---

## Step 11. Set up the AgentCore Gateway

The LLM calls the Gateway as a tool when it needs to understand what a Terraform argument means or which values it accepts — at most twice per resource type. The Gateway restricts searches to `developer.hashicorp.com`, `docs.aws.amazon.com`, and `registry.terraform.io` only. Search results are treated as untrusted text: the LLM uses them to understand arguments, never as instructions, and the generated code still has to pass all gates before it is used.

The Gateway uses IAM authorization: the runtime signs each request with its role (signing name `bedrock-agentcore`). No token or secret is stored. The runtime only sends signed requests to an https AWS address.

1. **Check the Gateway from your computer first.** From the repo root:
   ```powershell
   pip install "mcp>=1.9,<2" boto3 httpx
   python check_gateway.py --url https://<your-gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp
   ```
   You want `OK: the Gateway answers signed requests and its search stays on the allowed documentation sites`. If it prints `403`, your login needs `bedrock-agentcore:InvokeGateway` on the gateway. If the tool returns an access-denied error, the Gateway's service role needs `bedrock-agentcore:InvokeWebSearch` (the Gateway console creates that role for you).
2. **Role permission.** `iam/permissions-policy.json` has the `InvokeDocsGateway` statement. Make sure it has your gateway's ARN, then paste the updated policy into the runtime role (Step 6).
3. **Set the runtime environment variable.** Edit the runtime and add `GATEWAY_URL` = your gateway URL. Leave `GATEWAY_TOKEN` unset — the runtime signs its requests with its IAM role. Optional: `GATEWAY_REGION` (default: the runtime's region).
4. **Verify.** Run again in a new session. The runtime log should show `gateway: 1 tool(s): ['websearch___WebSearch']`. When the LLM used a search, the resource type's notes say `looked up the Terraform and AWS documentation (N searches)`. If the Gateway cannot be reached the log says why and the run continues without it.

Web Search costs a small amount per query, and it is available in us-east-1.

## About AgentCore Gateway

Everything that must be reliable (discovery, `terraform import`, the checks, saving to S3) is plain code. The Gateway gives the LLM real-time access to documentation while it tidies or repairs code — it is the only tool the LLM can call, and it is restricted to three domains so it cannot fetch arbitrary content.

Where Gateway would help next: **keeping the runtime role write-only on results.** A small Lambda behind a Gateway could do the saving, so the role that reads customer accounts could not write anywhere. That is not built yet.

Where it would not help: running Terraform. Terraform plus the AWS provider is about 700 MB unpacked, which does not fit a zip-based Lambda (250 MB unpacked). The runtime's isolated microVM is the right place for it.

---

## Later: cross-account

Once Step 9 passes, create the read-only role in the customer account (the CloudFormation template `importer-target-role.yaml` is not written yet), then in the Streamlit app choose **Add new customer...**, enter the role ARN, and type the external ID on each run. Discovery and import then run in the customer's account. Nothing else in the deployment changes.
