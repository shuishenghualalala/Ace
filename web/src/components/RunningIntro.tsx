import { useEffect, useState } from "react";
import { api } from "../api";

const FALLBACK_INTRO_LINES = [
  "Crew 可以把复杂需求拆成可跟踪的任务。",
  "Crew 支持单 Agent 和 Team 两种执行方式。",
  "Crew 会展示工具调用过程，方便你回看关键步骤。",
];

const FALLBACK_LOADING_STATUS = [
  "正在为您加速处理中......",
  "全力冲刺中......",
  "正在梳理关键步骤......",
];

function pickNextLine(lines: string[], current: string): string {
  if (lines.length === 0) return "";
  if (lines.length === 1) return lines[0];
  let next = current;
  while (next === current) {
    next = lines[Math.floor(Math.random() * lines.length)];
  }
  return next;
}

export default function RunningIntro() {
  const [introLines, setIntroLines] = useState<string[]>(FALLBACK_INTRO_LINES);
  const [statusLines, setStatusLines] = useState<string[]>(FALLBACK_LOADING_STATUS);
  const [intro, setIntro] = useState(() => pickNextLine(FALLBACK_INTRO_LINES, ""));
  const [status, setStatus] = useState(() => pickNextLine(FALLBACK_LOADING_STATUS, ""));

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([api.scenarioIntroLines(12), api.scenarioLoadingStatuses(12)])
      .then(([introResult, statusResult]) => {
        if (cancelled) return;
        if (introResult.status === "fulfilled") {
          const valid = introResult.value.map((item) => item.trim()).filter(Boolean);
          if (valid.length > 0) {
            setIntroLines(valid);
            setIntro((current) => pickNextLine(valid, current));
          }
        }
        if (statusResult.status === "fulfilled") {
          const valid = statusResult.value.map((item) => item.trim()).filter(Boolean);
          if (valid.length > 0) {
            setStatusLines(valid);
            setStatus((current) => pickNextLine(valid, current));
          }
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setIntro((current) => pickNextLine(introLines, current));
      setStatus((current) => pickNextLine(statusLines, current));
    }, 5000);
    return () => window.clearInterval(timer);
  }, [introLines, statusLines]);

  return (
    <div className="running-intro" aria-label="Crew 正在处理任务">
      <span className="running-intro__logo" aria-hidden="true">
        <svg className="nav-agent-logo running-intro__agent-logo" width="18" height="18" viewBox="3 3 18 18" aria-hidden="true">
          <path className="nav-agent-logo__blob" d="M5.2 13.2c0-4.5 2.9-6.9 6.8-6.9 4.5 0 7 2.8 7 6.2 0 3.8-2.5 5.5-7.2 5.5-4.3 0-6.6-1.4-6.6-4.8Z"/>
          <path className="nav-agent-logo__cap" d="M9 6.7c.7-1.1 1.7-1.7 3.1-1.7 1.3 0 2.3.5 3 1.5"/>
          <path className="nav-agent-logo__shine nav-agent-logo__shine--left" d="M9.6 10.8v1.9"/>
          <path className="nav-agent-logo__shine nav-agent-logo__shine--right" d="M14.4 10.8v1.9"/>
          <path className="nav-agent-logo__pixel" d="M18.8 8.2h1.5M19.55 7.45v1.5"/>
        </svg>
      </span>
      <span className="running-intro__text">
        <span className="running-intro__status">{status}</span>
        <span className="running-intro__ad">{intro}</span>
      </span>
    </div>
  );
}
