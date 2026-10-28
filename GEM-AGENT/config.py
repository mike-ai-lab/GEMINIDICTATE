from dataclasses import dataclass, field


@dataclass
class AudioConfig:
    input_rate: int = 16000
    output_rate: int = 24000
    chunk_ms: int = 40
    # Raised from 480 — speaker output leaks into mic and was causing
    # constant false barge-in triggers. 1500 requires genuine speech.
    bargein_rms_threshold: int = 1500
    bargein_confirm_blocks: int = 3
    output_device_index: int | None = None
    input_device_index: int | None = None


@dataclass
class ScreenConfig:
    capture_interval_s: float = 1.0
    idle_freeze_s: float = 3.0
    hash_change_threshold: int = 3
    max_dimension_px: int = 768
    jpeg_quality: int = 55
    # Lowered from 8.0s — agent needs faster visual feedback after actions
    min_send_gap_s: float = 2.0
    frame_queue_maxsize: int = 2


@dataclass
class SessionConfig:
    model: str = "models/gemini-2.5-flash-native-audio-latest"
    api_key_env_var: str = "GEMINI_API_KEY"
    ws_ping_interval_s: int = 20
    token_warn_threshold: int = 80000
    tokens_per_frame_estimate: int = 258


@dataclass
class DesktopConfig:
    # Mouse move duration in seconds
    move_duration: float = 0.4
    # Pause between actions (seconds)
    action_delay: float = 0.3
    # Max coordinate bounds (0 = auto-detect from screen)
    max_x: int = 0
    max_y: int = 0
    # Screenshot quality for observation frames
    obs_jpeg_quality: int = 70
    # How often the observer captures while a task runs (seconds)
    observe_interval_s: float = 1.5


@dataclass
class UIConfig:
    window_width: int = 330
    window_height: int = 560
    bg_color: str = "#04060a"
    accent_color: str = "#00f0ff"
    ai_color: str = "#a78bfa"
    text_color: str = "#c9d7e8"
    error_color: str = "#fb7185"
    amber_color: str = "#fbbf24"
    green_color: str = "#34d399"
    font_mono: str = "Consolas"
    font_ui: str = "Segoe UI"
    font_size: int = 9
    always_on_top: bool = True


@dataclass
class LogConfig:
    log_dir: str = "logs"
    max_log_files: int = 5
    max_log_size_kb: int = 512
    level: str = "DEBUG"


@dataclass
class AppConfig:
    audio: AudioConfig = field(default_factory=AudioConfig)
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    desktop: DesktopConfig = field(default_factory=DesktopConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    log: LogConfig = field(default_factory=LogConfig)
    app_name: str = "GEM-AGENT"


def load_config() -> AppConfig:
    return AppConfig()
