import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import "../styles/markdown.css";

/** Full durable result, independent of truncated legacy summaries/chat history. */
export default function CollaborationResult({ text, completed }: { text: string; completed: boolean }) {
  return (
    <section className="flow-node__result" aria-label="完整协作成果">
      <h3>{completed ? "完整成果（待人工复核）" : "未通过验收的原始输出"}</h3>
      <div className="md-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{
          img: ({ alt }) => <span>{alt || "图片未加载（只读成果）"}</span>,
          a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
        }}>{text}</ReactMarkdown>
      </div>
    </section>
  );
}
