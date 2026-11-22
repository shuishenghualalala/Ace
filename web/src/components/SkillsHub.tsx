import { useEffect, useState, type FormEvent } from "react";
import type { OptionalSkill, Skill, SkillStore } from "../types";
import { api, type SkillMarketSource } from "../api";
import browserKeycapSvg from "../assets/skills/browser-keycap.svg";
import clockKeycapSvg from "../assets/skills/clock-keycap.svg";
import cloudKeycapSvg from "../assets/skills/cloud-keycap.svg";
import codeKeycapSvg from "../assets/skills/code-keycap.svg";
import docKeycapSvg from "../assets/skills/doc-keycap.svg";
import gitKeycapSvg from "../assets/skills/git-keycap.svg";
import imageKeycapSvg from "../assets/skills/image-keycap.svg";
import legalKeycapSvg from "../assets/skills/legal-keycap.svg";
import marketingKeycapSvg from "../assets/skills/marketing-keycap.svg";
import mediaKeycapSvg from "../assets/skills/media-keycap.svg";
import monitorKeycapSvg from "../assets/skills/monitor-keycap.svg";
import pdfKeycapSvg from "../assets/skills/pdf-keycap.svg";
import sheetKeycapSvg from "../assets/skills/sheet-keycap.svg";
import slidesKeycapSvg from "../assets/skills/slides-keycap.svg";
import sparkKeycapSvg from "../assets/skills/spark-keycap.svg";
import translateKeycapSvg from "../assets/skills/translate-keycap.svg";
import travelKeycapSvg from "../assets/skills/travel-keycap.svg";
import writingKeycapSvg from "../assets/skills/writing-keycap.svg";

const SKILL_CATEGORIES = [
  "通用办公",
  "图像处理",
  "设计与开发",
  "经营管理",
  "人力资源",
  "音视频处理",
] as const;

type SkillIconKind =
  | "translate" | "image" | "slides" | "cloud" | "legal" | "marketing" | "media" | "monitor"
  | "travel"
  | "writing" | "pdf" | "sheet" | "doc" | "browser" | "git" | "clock" | "code" | "spark";

function skillIconKind(skill: { name: string; slug: string; display_name?: string; category?: string }): SkillIconKind {
  const text = `${skill.slug} ${skill.name} ${skill.display_name || ""} ${skill.category || ""}`.toLowerCase();
  if (
    text.includes("travel") || text.includes("flight") || text.includes("train")
    || text.includes("hotel") || text.includes("trip") || text.includes("商旅")
    || text.includes("航班") || text.includes("机票") || text.includes("火车")
    || text.includes("出差") || text.includes("酒店")
  ) return "travel";
  if (
    text.includes("audio") || text.includes("video") || text.includes("speech")
    || text.includes("transcrib") || text.includes("callassistant") || text.includes("phone")
    || text.includes("音频") || text.includes("视频") || text.includes("语音")
    || text.includes("电话")
  ) return "media";
  if (text.includes("translate") || text.includes("translation") || text.includes("翻译")) return "translate";
  if (text.includes("image") || text.includes("photo") || text.includes("vision") || text.includes("图片") || text.includes("图像")) return "image";
  if (text.includes("present") || text.includes("ppt") || text.includes("slide")) return "slides";
  if (text.includes("cloud") || text.includes("drive") || text.includes("云盘")) return "cloud";
  if (text.includes("legal") || text.includes("law") || text.includes("法")) return "legal";
  if (text.includes("marketing") || text.includes("market") || text.includes("营销")) return "marketing";
  if (text.includes("price") || text.includes("monitor")) return "monitor";
  if (
    text.includes("writing") || text.includes("writer") || text.includes("copy")
    || text.includes("content") || text.includes("email") || text.includes("mail")
    || text.includes("写作") || text.includes("文案") || text.includes("邮箱")
    || text.includes("邮件")
  ) return "writing";
  if (text.includes("pdf")) return "pdf";
  if (text.includes("excel") || text.includes("sheet") || text.includes("spread") || text.includes("data") || text.includes("analysis") || text.includes("数据")) return "sheet";
  if (text.includes("doc") || text.includes("word")) return "doc";
  if (
    text.includes("browser") || text.includes("web") || text.includes("search")
    || text.includes("research") || text.includes("insight") || text.includes("news")
    || text.includes("搜索") || text.includes("研究") || text.includes("新闻")
    || text.includes("日报") || text.includes("舆情")
  ) return "browser";
  if (text.includes("github") || text.includes("git")) return "git";
  if (text.includes("cron") || text.includes("automation")) return "clock";
  if (text.includes("code") || text.includes("coding") || text.includes("openai") || text.includes("model") || text.includes("编程")) return "code";
  return "spark";
}

const skillKeycapImages: Record<SkillIconKind, string> = {
  browser: browserKeycapSvg,
  clock: clockKeycapSvg,
  cloud: cloudKeycapSvg,
  code: codeKeycapSvg,
  doc: docKeycapSvg,
  git: gitKeycapSvg,
  image: imageKeycapSvg,
  legal: legalKeycapSvg,
  marketing: marketingKeycapSvg,
  media: mediaKeycapSvg,
  monitor: monitorKeycapSvg,
  pdf: pdfKeycapSvg,
  sheet: sheetKeycapSvg,
  slides: slidesKeycapSvg,
  spark: sparkKeycapSvg,
  translate: translateKeycapSvg,
  travel: travelKeycapSvg,
  writing: writingKeycapSvg,
};

function SkillKeycapIcon({ kind }: { kind: SkillIconKind }) {
  return (
    <img className={`skill-keycap-img skill-keycap-img--${kind}`} src={skillKeycapImages[kind]} alt="" />
  );
}

function SkillAvatar({ skill }: { skill: { name: string; slug: string; display_name?: string; category?: string } }) {
  const kind = skillIconKind(skill);
  return (
    <div className="hub-card__avatar" aria-hidden="true">
      <SkillKeycapIcon kind={kind} />
    </div>
  );
}

function skillTitle(skill: { name: string; display_name?: string }) {
  return skill.display_name || skill.name;
}

function skillDesc(skill: { description: string; description_zh?: string }) {
  return skill.description_zh || skill.description;
}

function CategoryFilter({
  value,
  onChange,
}: {
  value: string;
  onChange: (category: string) => void;
}) {
  const categories = ["全部", ...SKILL_CATEGORIES];
  return (
    <div className="hub__cats">
      {categories.map((category) => (
        <button
          key={category}
          className={"hub__cat" + (value === category ? " active" : "")}
          onClick={() => onChange(category)}
        >
          {category}
        </button>
      ))}
    </div>
  );
}

function InstalledCard({ skill, onUninstall }: { skill: Skill; onUninstall: (slug: string) => void }) {
  return (
    <div className="hub-card">
      <SkillAvatar skill={skill} />
      <div className="hub-card__body">
        <div className="hub-card__name">
          <span className="hub-card__title">{skillTitle(skill)}</span>
          <span className="hub-card__mini">{skill.category}</span>
        </div>
        <div className="hub-card__desc">{skillDesc(skill)}</div>
      </div>
      <div className="hub-card__action">
        {skill.source === "builtin" ? (
          <span className="hub-badge hub-badge--builtin">内置</span>
        ) : (
          <button
            className="hub-btn hub-btn--ghost"
            onClick={() => onUninstall(skill.slug)}
          >
            卸载
          </button>
        )}
      </div>
    </div>
  );
}

function OptionalCard({ skill, onInstall }: { skill: OptionalSkill; onInstall: (slug: string) => void }) {
  const [loading, setLoading] = useState(false);
  const handleInstall = async () => {
    setLoading(true);
    try { await onInstall(skill.slug); } finally { setLoading(false); }
  };
  return (
    <div className="hub-card">
      <SkillAvatar skill={skill} />
      <div className="hub-card__body">
        <div className="hub-card__name">
          <span className="hub-card__title">{skillTitle(skill)}</span>
          {skill.source === "local" && <span className="hub-badge hub-badge--local">本地</span>}
          <span className="hub-card__mini">{skill.category}</span>
        </div>
        <div className="hub-card__desc">{skillDesc(skill)}</div>
      </div>
      <div className="hub-card__action">
        <button
          className="hub-btn hub-btn--primary"
          onClick={handleInstall}
          disabled={loading}
        >
          {loading ? "安装中…" : "安装"}
        </button>
      </div>
    </div>
  );
}

export default function SkillsHub() {
  const [store, setStore] = useState<SkillStore | null>(null);
  const [q, setQ] = useState("");
  const [installedCategory, setInstalledCategory] = useState("全部");
  const [optionalCategory, setOptionalCategory] = useState("全部");
  const [sources, setSources] = useState<SkillMarketSource[]>([]);
  const [sourcePath, setSourcePath] = useState("");
  const [sourceBusy, setSourceBusy] = useState(false);
  const [sourceError, setSourceError] = useState("");
  const [importPath, setImportPath] = useState("");
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState("");

  const load = () => api.skillStore().then(setStore).catch(() => setStore({ installed: [], optional: [] }));
  const loadSources = () => api.skillSources()
    .then((result) => setSources(result.sources))
    .catch(() => setSources([]));

  useEffect(() => { void load(); void loadSources(); }, []);

  const handleAddSource = async (event: FormEvent) => {
    event.preventDefault();
    const path = sourcePath.trim();
    if (!path || sourceBusy) return;
    setSourceBusy(true);
    setSourceError("");
    try {
      await api.addSkillSource(path);
      setSourcePath("");
      await Promise.all([loadSources(), load()]);
    } catch (error) {
      setSourceError(error instanceof Error ? error.message : "无法添加市场目录");
    } finally {
      setSourceBusy(false);
    }
  };

  const handleImportSkill = async (event: FormEvent) => {
    event.preventDefault();
    const path = importPath.trim();
    if (!path || importBusy) return;
    setImportBusy(true);
    setImportError("");
    try {
      await api.importSkillDirectory(path);
      setImportPath("");
      await load();
    } catch (error) {
      setImportError(error instanceof Error ? error.message : "无法导入技能目录");
    } finally {
      setImportBusy(false);
    }
  };

  const handleRemoveSource = async (path: string) => {
    if (!confirm(`确认移除市场目录 "${path}" 吗？已安装技能不会被删除。`)) return;
    setSourceBusy(true);
    setSourceError("");
    try {
      await api.removeSkillSource(path);
      await Promise.all([loadSources(), load()]);
    } catch (error) {
      setSourceError(error instanceof Error ? error.message : "无法移除市场目录");
    } finally {
      setSourceBusy(false);
    }
  };

  const handleInstall = async (slug: string) => {
    await api.installSkill(slug);
    await load();
  };

  const handleUninstall = async (slug: string) => {
    if (!confirm(`确认卸载 skill "${slug}" 吗？`)) return;
    await api.uninstallSkill(slug);
    await load();
  };

  const installed = store?.installed ?? [];
  const optional = [...(store?.optional ?? []), ...(store?.local ?? [])];

  const filteredInstalled = installed.filter((s) => {
    const qLower = q.toLowerCase();
    const matchQ =
      !q ||
      skillTitle(s).toLowerCase().includes(qLower) ||
      skillDesc(s).toLowerCase().includes(qLower) ||
      s.name.toLowerCase().includes(qLower) ||
      s.slug.toLowerCase().includes(qLower) ||
      (s.aliases ?? []).some((a) => a.toLowerCase().includes(qLower));
    const matchCat = installedCategory === "全部" || s.category === installedCategory;
    return matchQ && matchCat;
  });
  const filteredOptional = optional.filter((s) => {
    const qLower = q.toLowerCase();
    const matchQ =
      !q ||
      skillTitle(s).toLowerCase().includes(qLower) ||
      skillDesc(s).toLowerCase().includes(qLower) ||
      s.name.toLowerCase().includes(qLower) ||
      s.slug.toLowerCase().includes(qLower) ||
      (s.aliases ?? []).some((a) => a.toLowerCase().includes(qLower));
    const matchCat = optionalCategory === "全部" || s.category === optionalCategory;
    return matchQ && matchCat;
  });

  return (
    <div className="hub">
      {/* Header */}
      <div className="hub__header">
        <div className="hub__header-left">
          <div className="hub__eyebrow">
            <span className="hub__pixel-dot" />
            Skill Deck
          </div>
          <h1 className="hub__title">技能</h1>
          <p className="hub__subtitle">技能卡册，给 Crew 装上顺手的小工具。</p>
        </div>
        <div className="hub__header-right">
          <div className="hub__search">
            <span className="hub__search-sprite" aria-hidden="true">
              <span />
            </span>
            <input
              placeholder="搜索技能"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
        </div>
      </div>

      <div className="hub__body">
        {/* 已安装 */}
        <section className="hub__section">
          <div className="hub__section-head">
            <span className="hub__section-title">已安装</span>
            <span className="hub__count">{filteredInstalled.length}</span>
          </div>
          <CategoryFilter value={installedCategory} onChange={setInstalledCategory} />
          {filteredInstalled.length === 0 ? (
            <p className="hub__empty">暂无已安装技能</p>
          ) : (
            <div className="hub__grid">
              {filteredInstalled.map((s) => (
                <InstalledCard key={s.slug} skill={s} onUninstall={handleUninstall} />
              ))}
            </div>
          )}
        </section>

        {/* 可安装 */}
        <section className="hub__section">
          <div className="hub__section-head">
            <span className="hub__section-title">可安装</span>
          </div>

          <CategoryFilter value={optionalCategory} onChange={setOptionalCategory} />

          {filteredOptional.length === 0 ? (
            <p className="hub__empty">{q ? "没有匹配的技能" : "所有可用技能已安装"}</p>
          ) : (
            <div className="hub__grid">
              {filteredOptional.map((s) => (
                <OptionalCard key={s.slug} skill={s} onInstall={handleInstall} />
              ))}
            </div>
          )}
        </section>

        <details className="hub__section hub__market">
          <summary className="hub__section-head">
            <span className="hub__section-title">市场目录</span>
            <span className="hub__count">{sources.length}</span>
          </summary>
          <p className="hub__empty">
            登记本机技能目录后，其中的技能会出现在「可安装」。变更需要管理员权限，Ace 不会执行目录中的代码。
          </p>
          <form className="hub__market-form" onSubmit={handleAddSource}>
            <input
              aria-label="市场目录路径"
              placeholder="绝对路径，例如 D:\\team-skills"
              value={sourcePath}
              onChange={(event) => setSourcePath(event.target.value)}
              disabled={sourceBusy}
            />
            <button
              className="hub-btn hub-btn--primary"
              type="submit"
              disabled={sourceBusy || !sourcePath.trim()}
            >
              添加目录
            </button>
          </form>
          {sourceError && <p className="hub__empty" role="alert">{sourceError}</p>}
          {sources.length === 0 ? (
            <p className="hub__empty">尚未登记市场目录</p>
          ) : (
            <ul className="hub__market-list">
              {sources.map((source) => (
                <li key={source.path} className="hub__market-item">
                  <span className="hub__market-label">{source.label}</span>
                  <code className="hub__market-path">{source.path}</code>
                  <button
                    type="button"
                    className="hub-btn hub-btn--ghost"
                    disabled={sourceBusy}
                    onClick={() => void handleRemoveSource(source.path)}
                  >
                    移除
                  </button>
                </li>
              ))}
            </ul>
          )}
          <p className="hub__empty">也可以直接导入一个已准备好、包含 SKILL.md 的技能目录。</p>
          <form className="hub__market-form" onSubmit={handleImportSkill}>
            <input
              aria-label="技能目录路径"
              placeholder="绝对路径，例如 D:\\my-skill"
              value={importPath}
              onChange={(event) => setImportPath(event.target.value)}
              disabled={importBusy}
            />
            <button
              className="hub-btn hub-btn--primary"
              type="submit"
              disabled={importBusy || !importPath.trim()}
            >
              导入技能
            </button>
          </form>
          {importError && <p className="hub__empty" role="alert">{importError}</p>}
        </details>
      </div>
    </div>
  );
}
