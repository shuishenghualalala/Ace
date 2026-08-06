---
name: process-doc
description: Document a business process — flowcharts, RACI, and SOPs. Use when formalizing
  a process that lives in someone's head, building a RACI to clarify who owns what,
  writing an SOP for a handoff or audit, or capturing the exceptions and edge cases
  of how work actually gets done.
argument-hint: <process name or description>
license: Apache-2.0
metadata:
  skillCategoryName: 通用办公
  zh_name: 业务流程文档
  zh_description: 记录业务流程——流程图、RACI 和 SOP。适用于将流程正式化、构建 RACI 明确职责、编写 SOP 用于交接或审计，以及记录实际工作中的异常和边界情况。
  query_examples: 记录业务流程, 创建流程图, 编写 SOP
---
<!-- Changes: added license field to frontmatter, removed broken ../../CONNECTORS.md reference, removed ~~knowledge base and ~~project tracker OpenClaw placeholder section. Original source: https://github.com/anthropics/knowledge-work-plugins/tree/main/operations/skills/process-doc (Apache-2.0) -->

# /process-doc

Document a business process as a complete standard operating procedure (SOP).

## Usage

```
/process-doc $ARGUMENTS
```

## How It Works

Walk me through the process — describe it, paste existing docs, or just tell me the name and I'll ask the right questions. I'll produce a complete SOP.

## Output

```markdown
## Process Document: [Process Name]
**Owner:** [Person/Team] | **Last Updated:** [Date] | **Review Cadence:** [Quarterly/Annually]

### Purpose
[Why this process exists and what it accomplishes]

### Scope
[What's included and excluded]

### RACI Matrix
| Step | Responsible | Accountable | Consulted | Informed |
|------|------------|-------------|-----------|----------|
| [Step] | [Who does it] | [Who owns it] | [Who to ask] | [Who to tell] |

### Process Flow
[ASCII flowchart or step-by-step description]

### Detailed Steps

#### Step 1: [Name]
- **Who**: [Role]
- **When**: [Trigger or timing]
- **How**: [Detailed instructions]
- **Output**: [What this step produces]

#### Step 2: [Name]
[Same format]

### Exceptions and Edge Cases
| Scenario | What to Do |
|----------|-----------|
| [Exception] | [How to handle it] |

### Metrics
| Metric | Target | How to Measure |
|--------|--------|----------------|
| [Metric] | [Target] | [Method] |

### Related Documents
- [Link to related process or policy]
```

## Tips

1. **Start messy** — You don't need a perfect description. Tell me how it works today and I'll structure it.
2. **Include the exceptions** — "Usually we do X, but sometimes Y" is the most valuable part to document.
3. **Name the people** — Even if roles change, knowing who does what today helps get the process right.
