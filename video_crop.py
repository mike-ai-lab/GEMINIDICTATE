"""
video_crop.py — Drag-to-crop video tool
Requires: Python 3, Pillow, FFmpeg in PATH
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import subprocess
import threading
import os
import tempfile
import json
from PIL import Image, ImageTk

# ── constants ────────────────────────────────────────────────────────────────
HANDLE_SIZE = 10          # half-size of corner/edge drag handles
MIN_CROP    = 16          # minimum crop dimension in px
PREVIEW_MAX = (900, 540)  # max canvas display size


class VideoCropApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video Crop Tool")
        self.resizable(True, True)
        self.configure(bg="#1e1e1e")

        # ── state ────────────────────────────────────────────────────────────
        self.video_path   = None
        self.video_w      = 0
        self.video_h      = 0
        self.duration     = 0.0
        self.frame_photo  = None   # ImageTk for canvas
        self.scale        = 1.0    # display scale  (display px / video px)

        # crop rect in VIDEO pixel coords
        self.crop = {"x": 0, "y": 0, "w": 0, "h": 0}

        # drag state
        self._drag_edge   = None   # which handle is being dragged
        self._drag_start  = (0, 0) # mouse position at drag start (canvas px)
        self._crop_start  = None   # copy of self.crop at drag start

        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        top = tk.Frame(self, bg="#1e1e1e")
        top.pack(fill="x", padx=8, pady=6)

        btn_style = {"bg": "#3a3a3a", "fg": "#ffffff", "relief": "flat",
                     "padx": 10, "pady": 4, "cursor": "hand2",
                     "activebackground": "#555", "activeforeground": "#fff"}

        tk.Button(top, text="Load Video", command=self._load_video,
                  **btn_style).pack(side="left", padx=4)
        tk.Button(top, text="Reset Crop", command=self._reset_crop,
                  **btn_style).pack(side="left", padx=4)
        tk.Button(top, text="Export Cropped Video", command=self._export,
                  **{**btn_style, "bg": "#1a6b3a"}).pack(side="left", padx=4)

        # dimensions label
        self.dim_var = tk.StringVar(value="No video loaded")
        tk.Label(top, textvariable=self.dim_var, bg="#1e1e1e", fg="#aaaaaa",
                 font=("Consolas", 10)).pack(side="right", padx=8)

        # canvas frame with scrollbars (in case window shrinks)
        cf = tk.Frame(self, bg="#111")
        cf.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        self.canvas = tk.Canvas(cf, bg="#111", cursor="crosshair",
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",        self._on_drag)
        self.canvas.bind("<ButtonRelease-1>",  self._on_release)
        self.canvas.bind("<Configure>",        self._on_canvas_resize)

        # progress bar (hidden until export)
        self.progress = ttk.Progressbar(self, mode="indeterminate", length=200)

    # ── video loading ─────────────────────────────────────────────────────────
    def _load_video(self):
        path = filedialog.askopenfilename(
            title="Select Video",
            filetypes=[("Video files", "*.mp4 *.mov *.avi *.mkv *.webm *.flv *.wmv"),
                       ("All files", "*.*")])
        if not path:
            return
        self.video_path = path
        self._probe_video()
        self._extract_frame()

    def _probe_video(self):
        """Use ffprobe to get width, height, duration."""
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", self.video_path
        ]
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for s in data.get("streams", []):
                if s.get("codec_type") == "video":
                    self.video_w   = int(s["width"])
                    self.video_h   = int(s["height"])
                    dur = s.get("duration") or data.get("format", {}).get("duration", "0")
                    self.duration  = float(dur)
                    break
        except Exception as e:
            messagebox.showerror("FFprobe error", str(e))

    def _extract_frame(self):
        """Extract one frame for preview."""
        if not self.video_w:
            return
        # seek to 10% if we have duration, otherwise grab very first frame
        seek_args = []
        if self.duration > 0.5:
            seek = self.duration * 0.1
            seek_args = ["-ss", f"{seek:.3f}"]

        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        cmd = (
            ["ffmpeg", "-y"]
            + seek_args
            + ["-i", self.video_path, "-vframes", "1", "-q:v", "2", tmp.name]
        )
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode != 0 or not os.path.getsize(tmp.name):
                raise RuntimeError(result.stderr.decode(errors="replace")[-400:])
            img = Image.open(tmp.name)
            self._raw_frame = img.copy()
            img.close()
            os.unlink(tmp.name)
            self._reset_crop()
            self._fit_canvas()
        except Exception as e:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
            messagebox.showerror("Frame extract error", str(e))

    # ── layout / scaling ──────────────────────────────────────────────────────
    def _fit_canvas(self):
        """Scale frame to fit inside PREVIEW_MAX, render it."""
        if not hasattr(self, "_raw_frame"):
            return
        cw = self.canvas.winfo_width()  or PREVIEW_MAX[0]
        ch = self.canvas.winfo_height() or PREVIEW_MAX[1]
        max_w = min(cw, PREVIEW_MAX[0])
        max_h = min(ch, PREVIEW_MAX[1])
        sx = max_w / self.video_w
        sy = max_h / self.video_h
        self.scale = min(sx, sy, 1.0)  # never upscale

        dw = int(self.video_w * self.scale)
        dh = int(self.video_h * self.scale)
        self.canvas.config(width=dw, height=dh)

        display = self._raw_frame.resize((dw, dh), Image.LANCZOS)
        self.frame_photo = ImageTk.PhotoImage(display)
        self._redraw()

    def _on_canvas_resize(self, _evt=None):
        self._fit_canvas()

    # ── crop helpers ──────────────────────────────────────────────────────────
    def _reset_crop(self):
        self.crop = {"x": 0, "y": 0, "w": self.video_w, "h": self.video_h}
        self._update_dim_label()
        self._redraw()

    def _update_dim_label(self):
        cw, ch = self.crop["w"], self.crop["h"]
        self.dim_var.set(
            f"Source: {self.video_w} × {self.video_h}  →  "
            f"Crop: {cw} × {ch}"
        )

    # ── drawing ───────────────────────────────────────────────────────────────
    def _redraw(self):
        self.canvas.delete("all")
        if self.frame_photo:
            self.canvas.create_image(0, 0, anchor="nw", image=self.frame_photo)

        if not self.video_w:
            return

        s  = self.scale
        cx = int(self.crop["x"] * s)
        cy = int(self.crop["y"] * s)
        cw = int(self.crop["w"] * s)
        ch = int(self.crop["h"] * s)
        x2 = cx + cw
        y2 = cy + ch

        # dark overlay outside crop (tkinter doesn't support alpha hex, use stipple)
        dw = int(self.video_w * s)
        dh = int(self.video_h * s)
        for rx1, ry1, rx2, ry2 in [
            (0,  0,  dw, cy),
            (0,  y2, dw, dh),
            (0,  cy, cx, y2),
            (x2, cy, dw, y2),
        ]:
            if rx2 > rx1 and ry2 > ry1:
                self.canvas.create_rectangle(
                    rx1, ry1, rx2, ry2,
                    fill="#000000", outline="", stipple="gray50")

        # crop border
        self.canvas.create_rectangle(cx, cy, x2, y2,
                                     outline="#00e5ff", width=2, tags="crop_rect")

        # handles: corners + edge midpoints
        for hx, hy, tag in self._handle_positions(cx, cy, x2, y2):
            self.canvas.create_rectangle(
                hx - HANDLE_SIZE, hy - HANDLE_SIZE,
                hx + HANDLE_SIZE, hy + HANDLE_SIZE,
                fill="#00e5ff", outline="#ffffff", width=1, tags=tag)

    @staticmethod
    def _handle_positions(cx, cy, x2, y2):
        mx, my = (cx + x2) // 2, (cy + y2) // 2
        return [
            (cx,  cy,  "tl"), (mx,  cy,  "tm"), (x2,  cy,  "tr"),
            (cx,  my,  "ml"),                    (x2,  my,  "mr"),
            (cx,  y2,  "bl"), (mx,  y2,  "bm"), (x2,  y2,  "br"),
        ]

    # ── drag interaction ──────────────────────────────────────────────────────
    def _hit_handle(self, mx, my):
        """Return which handle tag the mouse is near, or None."""
        s  = self.scale
        cx = int(self.crop["x"] * s)
        cy = int(self.crop["y"] * s)
        x2 = cx + int(self.crop["w"] * s)
        y2 = cy + int(self.crop["h"] * s)
        px, py = (cx + x2) // 2, (cy + y2) // 2

        for hx, hy, tag in self._handle_positions(cx, cy, x2, y2):
            if abs(mx - hx) <= HANDLE_SIZE + 4 and abs(my - hy) <= HANDLE_SIZE + 4:
                return tag
        return None

    def _on_press(self, evt):
        hit = self._hit_handle(evt.x, evt.y)
        if hit:
            self._drag_edge  = hit
            self._drag_start = (evt.x, evt.y)
            self._crop_start = dict(self.crop)
        else:
            self._drag_edge = None

    def _on_drag(self, evt):
        if not self._drag_edge or not self._crop_start:
            return
        s  = self.scale
        dx = (evt.x - self._drag_start[0]) / s
        dy = (evt.y - self._drag_start[1]) / s

        c  = dict(self._crop_start)
        tag = self._drag_edge

        # original right / bottom in video coords
        orig_x2 = c["x"] + c["w"]
        orig_y2 = c["y"] + c["h"]

        new_x, new_y = c["x"], c["y"]
        new_x2, new_y2 = orig_x2, orig_y2

        if "l" in tag:  new_x  = c["x"] + dx
        if "r" in tag:  new_x2 = orig_x2 + dx
        if "t" in tag:  new_y  = c["y"] + dy
        if "b" in tag:  new_y2 = orig_y2 + dy

        # clamp to video bounds
        new_x  = max(0, min(new_x,  self.video_w - MIN_CROP))
        new_y  = max(0, min(new_y,  self.video_h - MIN_CROP))
        new_x2 = max(new_x + MIN_CROP, min(new_x2, self.video_w))
        new_y2 = max(new_y + MIN_CROP, min(new_y2, self.video_h))

        # make even (FFmpeg prefers even dimensions)
        new_x  = int(new_x)  & ~1
        new_y  = int(new_y)  & ~1
        new_x2 = int(new_x2) & ~1
        new_y2 = int(new_y2) & ~1

        self.crop = {"x": new_x, "y": new_y,
                     "w": new_x2 - new_x, "h": new_y2 - new_y}
        self._update_dim_label()
        self._redraw()

    def _on_release(self, evt):
        self._drag_edge = None

    # ── export ────────────────────────────────────────────────────────────────
    def _export(self):
        if not self.video_path:
            messagebox.showwarning("No video", "Load a video first.")
            return

        base, ext = os.path.splitext(self.video_path)
        default_out = base + "_cropped" + ext
        out_path = filedialog.asksaveasfilename(
            title="Save Cropped Video",
            initialfile=os.path.basename(default_out),
            initialdir=os.path.dirname(default_out),
            defaultextension=ext,
            filetypes=[("Video files", "*.mp4 *.mov *.avi *.mkv"),
                       ("All files", "*.*")])
        if not out_path:
            return

        self.progress.pack(fill="x", padx=8, pady=4)
        self.progress.start(10)

        threading.Thread(target=self._run_ffmpeg,
                         args=(out_path,), daemon=True).start()

    def _run_ffmpeg(self, out_path):
        c = self.crop
        # FFmpeg crop filter: crop=w:h:x:y
        crop_filter = f"crop={c['w']}:{c['h']}:{c['x']}:{c['y']}"
        cmd = [
            "ffmpeg", "-y",
            "-i", self.video_path,
            "-vf", crop_filter,
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "copy",
            out_path
        ]
        try:
            subprocess.run(cmd, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.after(0, self._export_done, out_path, None)
        except subprocess.CalledProcessError as e:
            self.after(0, self._export_done, out_path, str(e))

    def _export_done(self, out_path, error):
        self.progress.stop()
        self.progress.pack_forget()
        if error:
            messagebox.showerror("Export failed", error)
        else:
            messagebox.showinfo("Done",
                f"Saved to:\n{out_path}\n\n"
                f"Crop: {self.crop['w']} × {self.crop['h']} "
                f"at ({self.crop['x']}, {self.crop['y']})")


# ── entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = VideoCropApp()
    app.geometry("960x620")
    app.mainloop()
