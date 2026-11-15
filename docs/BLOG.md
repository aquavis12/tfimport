# Automating Terraform Imports with Amazon Bedrock AgentCore

*by aquavis12 · November 15, 2026*

> 📹 **[Watch the demo](https://videos-hackthon-593845248500-us-west-1-an.s3.us-west-1.amazonaws.com/terraform-importer.mp4)** · 💻 **[github.com/aquavis12/tfimport](https://github.com/aquavis12/tfimport)**

**Terraform Importer** finds AWS resources that no IaC tool manages and brings them under Terraform automatically. It runs as an Amazon Bedrock AgentCore Runtime. It scans a region, drops anything already in Terraform state, CloudFormation, or created by AWS, and runs `terraform plan -generate-config-out` for the rest. An LLM then cleans up the code, following a skill stored in Agent Registry and looking up arguments through AgentCore Gateway. The LLM's code is used only if `fmt`, `validate`, and an import-only `plan` all pass.

---

## The problem

IaC has always been the right way to manage cloud infrastructure. The goal is simple: every AWS resource should live in a Terraform state file so the team can review changes, enforce standards, and avoid configuration drift.

The reality is messier. Resources get clicked together in the console, created by service wizards, or spun up during a proof of concept. `terraform import` can bring them under management, but only one resource at a time. For each one you look up the import ID, write the resource block by hand, run the import, fix the generated config, and repeat. For twenty resources across five types that is an afternoon of work. For a whole account region it simply never happens.

`terraform plan -generate-config-out` changed that. It writes resource blocks automatically for a list of IDs. But it still needs discovery (which resources exist and are not already managed?), and the generated code sometimes has provider errors that need fixing. That is the gap this tool fills.

---

## How it is built

The tool runs as an Amazon Bedrock AgentCore Runtime, a Python zip deployed directly with no container.

![Architecture: AgentCore Runtime, Agent Registry, Gateway, S3](images/tfimport.png)

```mermaid
flowchart LR
    A[Discovery\nScan region\nFilter state · CF · AWS-created] --> B[Import\ngenerate-config-out\nfmt · validate · plan]
    B --> C[Tidy\nLLM + import skill\nGateway for doc search]
    C --> D{All gates pass?\nfmt · validate · plan}
    D -->|Yes| E[S3: LLM code\nREADME · zip]
    D -->|No| F[S3: Terraform code\n+ needs-review notes]
```

**Discovery** lists every resource in the target region, then drops anything already in a Terraform state file, managed by CloudFormation, or created by AWS itself (default VPC, service-managed repos). What remains is genuinely unmanaged.

**Import** runs `terraform plan -generate-config-out` for each type, then `fmt`, `validate`, and `plan`. If the provider rejects placeholder values Terraform wrote (for example `maximum_message_size = 0` on an SNS topic), the importer strips those lines and re-checks.

**Tidy** sends the generated code to NVIDIA Nemotron Super 3 120B on Amazon Bedrock. The LLM uses the import skill from Agent Registry as its system prompt: naming conventions, tag rules, safety constraints, and the three gates every output must pass.

**Publish** saves validated `.tf` files, a terraform-docs README, and a zip to S3.

### Why AgentCore

- **Runtime** runs Terraform and the AWS provider in an isolated microVM. They are too large for a zip-based Lambda.
- **Registry** holds the import skill, so the LLM's instructions can change without rebuilding or redeploying the zip. If the registry is unreachable, the runtime uses the copy bundled in the zip.
- **Gateway** gives the LLM one tool, web search, limited to `developer.hashicorp.com`, `docs.aws.amazon.com`, and `registry.terraform.io`. It signs requests with the runtime's IAM role, so no token is stored.

![tfimport-skills registry: 1 approved record, auto-approval enabled](images/02-skill-registry.png)

### Safety

The LLM only touches the cleanup step. Discovery, import, the three gates, and writing to S3 are plain code. The LLM searches the docs only when it needs to know what an argument means, and at most twice per resource type. Search results are treated as untrusted text: they help it understand arguments and are never followed as instructions. Its code is used only if it contains just resource and import blocks for exactly the discovered IDs, and `fmt`, `validate`, and an import-only `plan` all pass. Otherwise Terraform's own output is kept.

---

## Running it

The Streamlit UI finds your AgentCore runtimes through the AWS control plane, so there is no ARN to paste. Pick a region and click **Find and Import**.

![Streamlit Import tab: terraform_importer READY, us-west-2](images/03-ui.png)

The first run downloads Terraform and the AWS provider and takes a few minutes. Later runs in the same session finish in under a minute.

---

## The result

The Results tab lets you browse past runs and download files from the browser:

![Streamlit Results tab: download buttons for each generated file](images/01-ui-results-tab.png)

Each run produces a timestamped folder in S3 with everything needed to bring the resources under Terraform:

![Generated README: SQS ready, SNS needs review, how-to-use steps](images/04-import-readme.png)

The SQS queue passed all three gates and is ready to apply.

To use the output:

```bash
unzip terraform-import-*.zip
terraform init
terraform plan    # N to import, 0 to add, 0 to change, 0 to destroy
terraform apply
```

Resources that lived outside IaC are now in Terraform state. The import blocks are only needed once. After `apply`, remove them and keep the resource blocks.

---

*MIT License · Copyright © 2026 aquavis12*
