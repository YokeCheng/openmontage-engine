import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

export type CouncilForgeScene = {
  scene_id: string;
  title: string;
  duration_seconds: number;
  narration: string;
  visual?: { type?: string; description?: string };
};

export type CouncilForgePlatformProps = {
  title: string;
  objective: string;
  format: "product_intro" | "knowledge_explainer";
  language: "zh-CN" | "en-US";
  scenes: CouncilForgeScene[];
  render: {
    aspect_ratio: "16:9" | "9:16";
    width: number;
    height: number;
    fps: number;
    duration_seconds: number;
  };
};

const palette = {
  night: "#14232D",
  monitor: "#263741",
  paper: "#E9EFF2",
  amber: "#E6A23C",
  green: "#49A38C",
  red: "#C65D57",
};

function currentScene(props: CouncilForgePlatformProps, seconds: number) {
  let cursor = 0;
  for (let index = 0; index < props.scenes.length; index += 1) {
    const scene = props.scenes[index];
    const end = cursor + scene.duration_seconds;
    if (seconds < end || index === props.scenes.length - 1) {
      return { scene, index, localSeconds: Math.max(0, seconds - cursor) };
    }
    cursor = end;
  }
  return { scene: props.scenes[0], index: 0, localSeconds: seconds };
}

const zhSceneTitles = ["打破误解", "唯一大脑", "分阶段执行", "强制审批", "可恢复交付"];

const nodeStyle = (active = false): React.CSSProperties => ({
  border: `1px solid ${active ? palette.green : "rgba(233,239,242,.18)"}`,
  background: active ? "rgba(73,163,140,.16)" : "rgba(233,239,242,.045)",
  borderRadius: 16,
  padding: "18px 20px",
  color: active ? palette.paper : "rgba(233,239,242,.66)",
  boxShadow: active ? "0 0 32px rgba(73,163,140,.16)" : "none",
});

const SceneGraphic: React.FC<{
  index: number;
  progress: number;
  vertical: boolean;
}> = ({ index, progress, vertical }) => {
  const visible = (step: number, total: number) => progress >= step / total;
  const panel: React.CSSProperties = {
    width: vertical ? "100%" : 650,
    minHeight: vertical ? 320 : 420,
    border: "1px solid rgba(233,239,242,.12)",
    borderRadius: 24,
    background: "rgba(8,18,24,.38)",
    padding: 34,
    display: "flex",
    flexDirection: "column",
    justifyContent: "center",
  };

  if (index === 0) {
    return (
      <div style={panel}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 18 }}>
          <div style={{ ...nodeStyle(), flex: 1, textAlign: "center", fontSize: 25 }}>通用大模型</div>
          <div style={{ position: "relative", width: 116, height: 42 }}>
            <div style={{ position: "absolute", top: 19, width: "100%", height: 3, background: palette.red }} />
            <div style={{ position: "absolute", left: 50, top: 2, color: palette.red, fontSize: 34, fontWeight: 700, transform: `scale(${visible(1, 3) ? 1 : 0})` }}>×</div>
          </div>
          <div style={{ ...nodeStyle(), flex: 1, textAlign: "center", fontSize: 25 }}>成片视频</div>
        </div>
        <div style={{ marginTop: 38, color: palette.amber, fontSize: 24, textAlign: "center", opacity: visible(2, 3) ? 1 : 0 }}>
          一次提示 ≠ 可交付流程
        </div>
      </div>
    );
  }
  if (index === 1) {
    return (
      <div style={panel}>
        <div style={{ ...nodeStyle(true), textAlign: "center", fontSize: 30, fontWeight: 700 }}>CouncilForge</div>
        <div style={{ height: 54, width: 2, background: palette.green, margin: "0 auto" }} />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 14 }}>
          {["读取 Brief", "做出决策", "推进阶段"].map((label, itemIndex) => (
            <div key={label} style={{ ...nodeStyle(visible(itemIndex + 1, 4)), textAlign: "center", fontSize: 21 }}>{label}</div>
          ))}
        </div>
        <div style={{ marginTop: 28, color: "rgba(233,239,242,.5)", textAlign: "center", fontSize: 18 }}>唯一模型与 Agent 决策中心</div>
      </div>
    );
  }
  if (index === 2) {
    const stages = ["研究", "方案", "脚本", "分镜", "素材", "剪辑", "合成", "交付"];
    return (
      <div style={panel}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
          {stages.map((label, itemIndex) => (
            <div key={label} style={{ ...nodeStyle(visible(itemIndex, stages.length)), textAlign: "center", fontSize: 20 }}>
              <div style={{ color: palette.amber, fontSize: 13, marginBottom: 8 }}>{String(itemIndex + 1).padStart(2, "0")}</div>
              {label}
            </div>
          ))}
        </div>
        <div style={{ marginTop: 26, height: 5, borderRadius: 10, background: "rgba(233,239,242,.1)", overflow: "hidden" }}>
          <div style={{ width: `${progress * 100}%`, height: "100%", background: palette.green }} />
        </div>
      </div>
    );
  }
  if (index === 3) {
    return (
      <div style={panel}>
        <div style={{ display: "flex", gap: 12, justifyContent: "center" }}>
          {["Review", "Checkpoint", "人工审批"].map((label, itemIndex) => (
            <div key={label} style={{ ...nodeStyle(visible(itemIndex, 3)), fontSize: 21, minWidth: 150, textAlign: "center" }}>{label}</div>
          ))}
        </div>
        <div style={{ margin: "38px auto 0", width: 280, height: 90, border: `2px solid ${palette.amber}`, borderRadius: 18, display: "grid", placeItems: "center", color: palette.amber, fontSize: 25, transform: `translateY(${visible(2, 3) ? 0 : -28}px)` }}>
          等你确认后继续
        </div>
        <div style={{ marginTop: 26, color: "rgba(233,239,242,.5)", textAlign: "center", fontSize: 18 }}>审批是强制 Gate，不是提醒</div>
      </div>
    );
  }
  return (
    <div style={panel}>
      <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <div style={{ ...nodeStyle(true), flex: 1, textAlign: "center", fontSize: 22 }}>PostgreSQL<br/><span style={{ fontSize: 14, opacity: .62 }}>业务状态</span></div>
        <div style={{ color: palette.green, fontSize: 32 }}>→</div>
        <div style={{ ...nodeStyle(true), flex: 1, textAlign: "center", fontSize: 22 }}>Checkpoint<br/><span style={{ fontSize: 14, opacity: .62 }}>断点恢复</span></div>
        <div style={{ color: palette.green, fontSize: 32 }}>→</div>
        <div style={{ ...nodeStyle(true), flex: 1, textAlign: "center", fontSize: 22 }}>MinIO<br/><span style={{ fontSize: 14, opacity: .62 }}>长期产物</span></div>
      </div>
      <div style={{ marginTop: 42, borderTop: "1px solid rgba(233,239,242,.12)", paddingTop: 28, color: palette.amber, textAlign: "center", fontSize: 23, opacity: visible(2, 3) ? 1 : .2 }}>
        失败不重跑，从最近阶段继续
      </div>
    </div>
  );
};

export const CouncilForgePlatform: React.FC<CouncilForgePlatformProps> = (
  props,
) => {
  const frame = useCurrentFrame();
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const seconds = frame / fps;
  const { scene, index, localSeconds } = currentScene(props, seconds);
  const vertical = height > width;
  const entrance = spring({
    frame: localSeconds * fps,
    fps,
    config: { damping: 20, stiffness: 120, mass: 1 },
  });
  const progress = frame / Math.max(1, durationInFrames - 1);
  const sceneProgress = Math.min(1, localSeconds / Math.max(.1, scene.duration_seconds));
  const drift = interpolate(progress, [0, 1], [-4, 8]);
  const label = props.format === "product_intro" ? "PRODUCT FILM" : "KNOWLEDGE FILM";

  return (
    <AbsoluteFill
      style={{
        color: palette.paper,
        backgroundColor: palette.night,
        fontFamily:
          'Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
        overflow: "hidden",
      }}
    >
      <AbsoluteFill
        style={{
          background: `radial-gradient(circle at ${22 + drift}% 20%, rgba(73,163,140,.27), transparent 34%), radial-gradient(circle at 78% ${74 - drift}%, rgba(230,162,60,.18), transparent 31%), linear-gradient(145deg, ${palette.night}, ${palette.monitor})`,
        }}
      />
      <div
        style={{
          position: "absolute",
          inset: vertical ? "6% 7%" : "7% 6%",
          border: "1px solid rgba(233,239,242,.16)",
          borderRadius: vertical ? 28 : 34,
          padding: vertical ? "7% 7%" : "5% 6%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          backgroundColor: "rgba(20,35,45,.55)",
          boxShadow: "0 30px 90px rgba(0,0,0,.3)",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ fontSize: vertical ? 20 : 24, letterSpacing: 5, color: palette.amber }}>
            COUNCILFORGE · {label}
          </div>
          <div style={{ fontSize: vertical ? 20 : 22, color: "rgba(233,239,242,.62)" }}>
            {String(index + 1).padStart(2, "0")} / {String(props.scenes.length).padStart(2, "0")}
          </div>
        </div>

        <div
          style={{
            display: "flex",
            flexDirection: vertical ? "column" : "row",
            alignItems: vertical ? "stretch" : "center",
            justifyContent: "space-between",
            gap: vertical ? 34 : 54,
          }}
        >
          <div style={{ transform: `translateY(${interpolate(entrance, [0, 1], [52, 0])}px)`, opacity: entrance, flex: 1, maxWidth: vertical ? "100%" : 690 }}>
            <div style={{ width: vertical ? 74 : 92, height: 8, borderRadius: 20, backgroundColor: index % 3 === 1 ? palette.green : palette.amber, marginBottom: vertical ? 28 : 34 }} />
            <h1 style={{ fontSize: vertical ? 66 : 78, lineHeight: 1.03, letterSpacing: -2, margin: 0, fontWeight: 680 }}>
              {props.language === "zh-CN" ? zhSceneTitles[index] ?? scene.title : scene.title}
            </h1>
            <p style={{ fontSize: vertical ? 31 : 35, lineHeight: 1.45, color: "rgba(233,239,242,.8)", margin: `${vertical ? 28 : 32}px 0 0` }}>
              {scene.narration}
            </p>
          </div>
          <SceneGraphic index={index} progress={sceneProgress} vertical={vertical} />
        </div>

        <div>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: vertical ? 18 : 22, color: "rgba(233,239,242,.55)", marginBottom: 16 }}>
            <span>{props.title}</span>
            <span>{Math.floor(seconds / 60).toString().padStart(2, "0")}:{Math.floor(seconds % 60).toString().padStart(2, "0")}</span>
          </div>
          <div style={{ height: 8, borderRadius: 10, backgroundColor: "rgba(233,239,242,.12)", overflow: "hidden" }}>
            <div style={{ width: `${progress * 100}%`, height: "100%", backgroundColor: palette.green }} />
          </div>
        </div>
      </div>
    </AbsoluteFill>
  );
};
