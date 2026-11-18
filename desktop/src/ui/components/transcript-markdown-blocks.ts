import { parse, postprocess, preprocess } from 'micromark';
import { gfm } from 'micromark-extension-gfm';
import { math } from 'micromark-extension-math';
import { preprocessStreamMarkdown } from '../markdown-stream';

type Token = ReturnType<typeof postprocess>[number][1];
interface Range {
  start: number;
  end: number;
}

interface FlowBlock {
  token: Token;
  blankAfter: boolean;
  fences: number;
  independentBrackets: Range[];
}

function scanFlow(source: string): FlowBlock[] {
  const events = postprocess(parse({ extensions: [gfm(), math()] })
    .document().write(preprocess()(source, undefined, true)));
  const blocks: FlowBlock[] = [];
  const stack: Token[] = [];
  let block: FlowBlock | undefined;
  for (const [kind, token] of events) {
    if (kind === 'exit') {
      stack.pop();
      continue;
    }
    const { start, end } = token;
    const type: string = token.type;
    const parent = stack.at(-1);
    if (!parent) {
      if (type === 'lineEndingBlank') {
        if (block) block.blankAfter = true;
      } else if (type !== 'linePrefix' && type !== 'lineEnding') {
        block = { token, blankAfter: false, fences: 0, independentBrackets: [] };
        blocks.push(block);
      }
    }
    if (block) {
      if (parent === block.token && (type === 'codeFencedFence' || type === 'mathFlowFence')) {
        block.fences += 1;
      }
      if (type === 'codeText' || type === 'codeFenced' || type === 'codeIndented' || type === 'taskListCheck') {
        block.independentBrackets.push({ start: start.offset, end: end.offset });
      }
      if (type === 'resource' && (parent?.type === 'link' || parent?.type === 'image')) {
        const bracket = parent.start.offset + (parent.type === 'image' ? 1 : 0);
        block.independentBrackets.push({ start: bracket, end: bracket + 1 }, { start: start.offset, end: end.offset });
      }
    }
    stack.push(token);
  }
  return blocks;
}

function completeLineEnd(source: string, after: number): number {
  let at = after;
  while (source[at] === ' ' || source[at] === '\t') at += 1;
  if (source[at] === '\r' && source[at + 1] === '\n') return at + 2;
  if (source[at] === '\n' || source[at] === '\r') return at + 1;
  return 0;
}

function mayDependOnFutureDefinitions(source: string, from: number, to: number, ranges: Range[]): boolean {
  let bracket = source.indexOf('[', from);
  if (bracket === -1 || bracket >= to) return false;
  ranges.sort((left, right) => left.start - right.start);
  let range = 0;
  for (; bracket !== -1 && bracket < to; bracket = source.indexOf('[', bracket + 1)) {
    // Wiki substitutions can cross a line or block boundary, even inside code.
    if (source[bracket + 1] === '[') return true;
    while (ranges[range] && ranges[range]!.end <= bracket) range += 1;
    if (!ranges[range] || ranges[range]!.start > bracket) return true;
  }
  return false;
}

/**
 * Find the part of the mutable Markdown suffix that following stream data
 * cannot change. Block boundaries come from the same syntax as the renderer.
 * Definitions and open delimiters stay in the mutable suffix so they retain
 * the preceding context needed for reference resolution and stream repair.
 */
export function stableMarkdownPrefix(source: string): number {
  const lastPossibleCr = source.length - (source.endsWith('\r') ? 2 : 1);
  const lastCr = lastPossibleCr >= 0 ? source.lastIndexOf('\r', lastPossibleCr) : -1;
  const completeLength = Math.max(source.lastIndexOf('\n'), lastCr) + 1;
  if (completeLength === 0) return 0;

  const complete = source.slice(0, completeLength);
  const blocks = scanFlow(complete);
  let cut = 0;
  for (const [index, block] of blocks.entries()) {
    const { end } = block.token;
    const type: string = block.token.type;
    const throughLine = completeLineEnd(complete, end.offset);
    if (throughLine <= cut) break;

    const hasNextBlock = index + 1 < blocks.length;
    const lineBlock = type === 'atxHeading' || type === 'setextHeading' || type === 'thematicBreak';
    const closedFence = (type === 'codeFenced' || type === 'mathFlow') && block.fences === 2;
    // A list or indented code block can continue past blank lines. A complete
    // next top-level block supplies the proof that the container has ended.
    if (!lineBlock && !closedFence && !hasNextBlock && !(block.blankAfter && (type === 'content' || type === 'table'))) {
      break;
    }
    if (mayDependOnFutureDefinitions(complete, cut, throughLine, block.independentBrackets)) break;
    const segment = complete.slice(cut, throughLine);
    // This literal marker can match a later Wiki link's generated placeholder.
    if (segment.includes('〈WIKI_LINK_') || preprocessStreamMarkdown(segment, true) !== segment) break;
    // Compact one-line tables are expanded before parsing in the renderer.
    // At a touching boundary, leave a potential expansion to the whole tail.
    if (!block.blankAfter && type === 'content' && /^\s*\|.*\|\s*\|/m.test(segment)) break;

    cut = throughLine;
  }
  return cut;
}
