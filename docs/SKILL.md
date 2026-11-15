---
name: import-tf
description: Clean Terraform generated from existing AWS resources. Naming, module layout, tags, safety, and the fmt, validate, and plan gates.
---

# Terraform import best practices

Use this skill when turning existing AWS resources into Terraform.

## Naming
- Name each resource from its `Name` tag first, using lowercase snake_case. Fall back to the existing name.
- Keep names unique within the file. Add a short suffix from the resource ID if two names collide.
- Never rename a resource that already has a Terraform address in state. Report the change in notes instead.

## Tags and values
- Keep every tag. Never remove or rename a tag without listing the change in notes.
- Replace hardcoded account IDs, ARNs, and region names with `data` sources or variables where it is safe.
- Do not add secrets or credentials to the code.

## Module layout
- Group resources into a module only when they share a lifecycle, such as a VPC with its subnets and route tables.
- Keep modules small. One module per logical stack.

## Safety
- Import only the resource types and IDs you were given. They are already filtered to resources not managed by Terraform, CloudFormation, or AWS. Do not guess other IDs.
- If a decision is uncertain, add it to `notes` and leave the code as generated.
- Keep `lifecycle` blocks out of the output unless they already exist in the generated code.

## Gates
Every import must pass these checks before it is reported as ready:
1. `terraform fmt` clean.
2. `terraform validate` passes.
3. `terraform plan` shows `no_changes` for the imported resources.

If a gate fails, return the stage and the error in the output. Do not report the import as ready.

## Output
Return the resource blocks and the import blocks for the resource type you were given, and a short list of notes about what you changed or were unsure about.
