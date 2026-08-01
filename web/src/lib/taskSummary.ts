function stripInlineMarkdownNoise(text: string): string {
  return String(text || "")
    .replace(
      /(?:^|\s)#{2,6}\s*(验证执行清单|五个维度结论|风险处置建议|产出文件|下一负责人|下一动作|风险\/阻塞|验证总览|方案要点|执行详情|日志)(?:\s|[:：]).*$/i,
      " ",
    )
    .replace(/\s*\|[^。；\n]*(?:\|[^。；\n]*){2,}/g, " ")
    .replace(/\s*[-|]{3,}\s*/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function plainText(text: string): string {
  const normalized = String(text || "")
    .split(/\r?\n/)
    .filter((line) => {
      const trimmed = line.trim();
      if (!trimmed) return true;
      if (/^\|.*\|$/.test(trimmed)) return false;
      if (/^:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+$/.test(trimmed)) return false;
      return true;
    })
    .join(" ")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/\s+/g, " ");
  return stripInlineMarkdownNoise(normalized);
}

function compactText(text: string, max: number): string {
  const normalized = plainText(text);
  if (!normalized) return "";
  return normalized.length > max ? `${normalized.slice(0, max - 1)}...` : normalized;
}

function splitReadableItems(text: string): string[] {
  const normalized = plainText(text);
  if (!normalized) return [];
  const semicolonParts = normalized
    .split(/[；;]\s*/)
    .map((part) => part.trim())
    .filter(Boolean);
  const baseParts = semicolonParts.length > 1 ? semicolonParts : [normalized];
  return baseParts.flatMap((part) => {
    if (part.length <= 90) return [part];
    const sentenceParts = part
      .split(/(?<=[。！？!?])\s*/)
      .map((item) => item.trim())
      .filter(Boolean);
    return sentenceParts.length > 1 ? sentenceParts : [part];
  });
}

function cleanSummaryLine(line: string): string {
  return plainText(line)
    .replace(/^(当前成果|执行结果)\s+/i, "")
    .replace(/^\d+[.)、]\s*/, "")
    .replace(/^[-*+]\s*/, "")
    .trim();
}

export function compactTaskSummary(text: string, max = 180): string {
  const summary = compactTaskSummaryItems(text, 3, max).join("；");
  return summary.length > max ? `${summary.slice(0, max - 1)}...` : summary;
}

export function compactTaskSummaryItems(text: string, maxItems = 4, maxItemLength = 120): string[] {
  const lines = String(text || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => {
      if (/^```/.test(line)) return false;
      if (/^\|.*\|$/.test(line)) return false;
      if (/^:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+$/.test(line)) return false;
      if (/^#{1,6}\s*(当前成果|执行结果|验证总览|方案要点|下一负责人|下一动作)\s*$/i.test(line)) return false;
      return true;
    });
  const source = lines.map(cleanSummaryLine).filter(Boolean);
  const items = source.flatMap(splitReadableItems).filter(Boolean);
  const fallback = splitReadableItems(text);
  return (items.length > 0 ? items : fallback)
    .slice(0, maxItems)
    .map((item) => compactText(item, maxItemLength))
    .filter(Boolean);
}

export function plainTaskText(text: string): string {
  return plainText(text);
}

export function compactTaskText(text: string, max = 90): string {
  return compactText(text, max);
}
