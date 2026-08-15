---
name: html-to-pdf
featured: true
description: Convert a bounded local HTML file to a new PDF inside Ace's managed
  OS sandbox. Use for static HTML reports that do not require scripts, remote
  resources, forms, frames, navigation, popups, or downloads.
metadata:
  version: 1.1.0
  zh_name: HTML转PDF
  zh_description: 在 Ace 受管沙箱中将有界静态 HTML 文件安全转换为新 PDF。
  mobileclaw:
    emoji: 📄
    requires:
      bins:
        - node
      env: []
---

# Hardened HTML-to-PDF conversion

Run the converter only through Ace's managed terminal:

```bash
node scripts/run.cjs input.html new-output.pdf
node scripts/run.cjs input.html new-output.pdf A4 '{"landscape":true,"margin":{"top":"12mm"}}'
```

Do not set `ACE_SANDBOX` manually. The marker is a managed-runtime routing
assertion, not a standalone proof that a directly launched host process is
sandboxed.
The examples assume Ace has pinned the `node` command. Deployments whose managed
PATH does not contain that trusted Node must invoke `scripts/run.cjs` with the
approved absolute Node path; user-controlled PATH lookup is unsupported.

Security and compatibility constraints:

- Input must be a regular, non-symlink UTF-8 local file of at most 2 MiB.
- URL inputs and all `file:`, network, relative, blob, script, frame, form,
  navigation, popup, download, and external resource behavior are denied.
- Only bounded inline styles and fragment references are accepted. `data:`
  resources and local fonts are denied with every other subresource; JavaScript
  and browser networking remain disabled.
- The output must be a new `.pdf` path; existing files are never overwritten.
- Formats: A3, A4, A5, Legal, Letter, or Tabloid.
- Options: `landscape`, `printBackground`, `scale`, `margin`, and
  `avoidChartBreak`. Header/footer templates and arbitrary Puppeteer options are
  intentionally unsupported.
- The converter never searches PATH, NVM/FNM, or environment variables for Node
  or Chromium and never downloads a browser at request time.
- Missing Ace/Chromium sandbox support, time/resource limits, cleanup failures,
  and browser crashes fail closed without publishing output.

Deployment must run `npm ci --ignore-scripts` in this skill directory and provide
a system-installed Chrome, Chromium, or Edge at one of the fixed paths encoded in
`scripts/convert.cjs`.
