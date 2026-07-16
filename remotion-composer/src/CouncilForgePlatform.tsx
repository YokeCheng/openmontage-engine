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
            transform: `translateY(${interpolate(entrance, [0, 1], [52, 0])}px)`,
            opacity: entrance,
            maxWidth: vertical ? "100%" : "82%",
          }}
        >
          <div
            style={{
              width: vertical ? 74 : 92,
              height: 8,
              borderRadius: 20,
              backgroundColor: index % 3 === 1 ? palette.green : palette.amber,
              marginBottom: vertical ? 38 : 44,
            }}
          />
          <h1
            style={{
              fontSize: vertical ? 76 : 112,
              lineHeight: 1.03,
              letterSpacing: -3,
              margin: 0,
              fontWeight: 680,
            }}
          >
            {scene.title}
          </h1>
          <p
            style={{
              fontSize: vertical ? 35 : 44,
              lineHeight: 1.45,
              color: "rgba(233,239,242,.8)",
              margin: `${vertical ? 34 : 42}px 0 0`,
              maxWidth: vertical ? "100%" : "88%",
            }}
          >
            {scene.narration}
          </p>
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
