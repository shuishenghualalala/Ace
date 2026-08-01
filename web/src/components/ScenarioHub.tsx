import { useEffect, useState } from "react";
import type { Scenario, SubScenario } from "../types";
import { api } from "../api";

interface Props {
  /** 点击某个细分玩法：把 query 预填进输入框 + 记录绑定 */
  onPick: (sub: SubScenario, parent: Scenario) => void;
}

const BATCH = 4;

/**
 * 场景化推荐：首页展示一批经典场景卡 + 「换一换」；
 * 选中某场景后在下方展开它的细分玩法 chip，点击即预填提示词。
 */
export default function ScenarioHub({ onPick }: Props) {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);

  const load = () => {
    api.scenarios(BATCH)
      .then((items) => {
        setScenarios(items);
        setActiveId(null);
      })
      .catch(() => setScenarios([]));
  };

  useEffect(load, []);

  if (scenarios.length === 0) return null;

  const active = scenarios.find((s) => s.id === activeId) ?? null;

  return (
    <div className="scenario-hub">
      <div className="scenario-hub__cards">
        {scenarios.map((s) => (
          <button
            key={s.id}
            className={"scenario-card" + (s.id === activeId ? " scenario-card--active" : "")}
            title={s.description}
            onClick={() => setActiveId((prev) => (prev === s.id ? null : s.id))}
          >
            {s.icon && <span className="scenario-card__icon">{s.icon}</span>}
            <span>{s.title}</span>
          </button>
        ))}
        <button className="scenario-card scenario-card--refresh" onClick={load} title="换一批场景">
          <span className="scenario-card__icon">🔄</span>
          <span>换一换</span>
        </button>
      </div>

      {active && active.items.length > 0 && (
        <div className="scenario-hub__items">
          {active.items.map((item) => (
            <button
              key={item.id}
              className="scenario-item"
              title={item.query}
              onClick={() => onPick(item, active)}
            >
              {item.title} ↘
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
