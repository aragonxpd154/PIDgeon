"""Decodificador de vídeo/áudio em tempo real (PyAV/FFmpeg) a partir de um TS filtrado."""

import array
import collections
import queue
import threading
import time

try:
    import av
    HAVE_AV = True
except Exception:  # pragma: no cover
    av = None
    HAVE_AV = False

from .audio_out import open_audio


class AudioThread(threading.Thread):
    def __init__(self, on_levels, on_error):
        super().__init__(daemon=True, name="pidgeon-audio")
        self.q = queue.Queue(maxsize=200)
        self.stop_ev = threading.Event()
        self.volume = 1.0
        self.muted = False
        self.on_levels = on_levels
        self.on_error = on_error
        self.out = None
        self.backend = None

    def push(self, pcm):
        try:
            self.q.put_nowait(pcm)
        except queue.Full:
            pass

    def run(self):
        try:
            self.out = open_audio(48000, 2)
            self.backend = self.out.name
        except Exception as exc:
            self.on_error("Áudio indisponível: %s" % exc)
            self.out = None
        while not self.stop_ev.is_set():
            try:
                pcm = self.q.get(timeout=0.3)
            except queue.Empty:
                continue
            a = array.array("h")
            a.frombytes(pcm)
            # medidores de nível (pico por canal, amostragem 1:8)
            l = max((abs(v) for v in a[0::16]), default=0)
            r = max((abs(v) for v in a[1::16]), default=0)
            self.on_levels(l / 32768.0, r / 32768.0)
            if self.out is None:
                continue
            if self.muted:
                pcm = bytes(len(pcm))
            elif self.volume != 1.0:
                v = self.volume
                a = array.array("h", (max(-32768, min(32767, int(s * v))) for s in a))
                pcm = a.tobytes()
            try:
                self.out.write(pcm)
            except Exception as exc:
                self.on_error("Erro de áudio: %s" % exc)
                self.out = None
        if self.out:
            self.out.close()

    def stop(self):
        self.stop_ev.set()


class Decoder(threading.Thread):
    """Lê o TS de `pipe` (file-like bloqueante), decodifica e entrega quadros RGB pacificados."""

    def __init__(self, pipe, on_frame, on_info, on_levels, on_error, audio=True,
                 deinterlace=True, max_width=1920, latency=0.45):
        super().__init__(daemon=True, name="pidgeon-decoder")
        self.pipe = pipe
        self.on_frame = on_frame
        self.on_info = on_info
        self.on_error = on_error
        self.want_audio = audio
        self.deinterlace = deinterlace
        self.max_width = max_width
        self.latency = latency
        self.stop_ev = threading.Event()
        self.audio = AudioThread(on_levels, on_error) if audio else None
        self.frames = 0
        self.dropped = 0
        self.fps_hist = collections.deque(maxlen=60)
        self.target_size = None

    def stop(self):
        self.stop_ev.set()
        if self.audio:
            self.audio.stop()

    def _build_deint(self, stream):
        g = av.filter.Graph()
        src = g.add_buffer(template=stream)
        yadif = g.add("yadif", "mode=0:parity=auto:deint=1")
        sink = g.add("buffersink")
        src.link_to(yadif)
        yadif.link_to(sink)
        g.configure()
        return g

    def _size_for(self, frame):
        w, h = frame.width, frame.height
        sar = frame.sample_aspect_ratio if hasattr(frame, "sample_aspect_ratio") else None
        dw = w
        try:
            if sar and float(sar) > 0 and abs(float(sar) - 1) > 0.01:
                dw = int(round(w * float(sar)))
        except Exception:
            pass
        tw = self.target_size[0] if self.target_size else self.max_width
        tw = max(160, min(tw, self.max_width, dw))
        th = int(round(h * tw / dw)) & ~1
        return tw & ~1, max(2, th)

    def run(self):
        if not HAVE_AV:
            self.on_error("PyAV não está instalado — vídeo indisponível")
            return
        if self.audio:
            self.audio.start()
        try:
            container = av.open(self.pipe, format="mpegts", mode="r",
                                options={"probesize": "3000000", "analyzeduration": "2000000",
                                         "fflags": "+discardcorrupt"})
        except Exception as exc:
            self.on_error("Não foi possível abrir o stream para decodificação: %s" % exc)
            return
        vs = container.streams.video[0] if container.streams.video else None
        as_ = container.streams.audio[0] if (container.streams.audio and self.want_audio) else None
        streams = [s for s in (vs, as_) if s is not None]
        if not streams:
            self.on_error("Nenhum stream de vídeo/áudio decodificável no serviço")
            return
        info = {}
        if vs is not None:
            vs.thread_type = "AUTO"
            cc = vs.codec_context
            info.update(vcodec=cc.name, vprofile=getattr(cc, "profile", None) or "",
                        width=cc.width, height=cc.height,
                        fps=float(vs.average_rate) if vs.average_rate else None)
        if as_ is not None:
            ac = as_.codec_context
            info.update(acodec=ac.name, arate=ac.sample_rate, achannels=ac.channels if hasattr(ac, "channels") else None,
                        alayout=getattr(getattr(ac, "layout", None), "name", ""))
        self.on_info(info)
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000) if as_ is not None else None
        deint = None
        deint_checked = False
        base = None
        last_info = 0
        try:
            for packet in container.demux(*streams):
                if self.stop_ev.is_set():
                    break
                try:
                    frames = packet.decode()
                except Exception:
                    continue
                for frame in frames:
                    if self.stop_ev.is_set():
                        break
                    if packet.stream is as_:
                        try:
                            for rf in resampler.resample(frame):
                                self.audio.push(bytes(rf.planes[0])[:rf.samples * 4])
                        except Exception:
                            pass
                        continue
                    # ---- vídeo
                    interlaced = bool(getattr(frame, "interlaced_frame", False))
                    if self.deinterlace and interlaced and not deint_checked:
                        deint_checked = True
                        try:
                            deint = self._build_deint(vs)
                        except Exception as exc:
                            self.on_error("Desentrelaçador indisponível: %s" % exc)
                            deint = None
                    out_frames = [frame]
                    if deint is not None and self.deinterlace:
                        try:
                            deint.push(frame)
                            out_frames = []
                            while True:
                                try:
                                    out_frames.append(deint.pull())
                                except Exception:
                                    break
                        except Exception:
                            out_frames = [frame]
                    for vf in out_frames:
                        ft = vf.time
                        now = time.monotonic()
                        if ft is not None:
                            if base is None or abs((base + ft) - now) > 3.0:
                                base = now + self.latency - ft
                            wait = base + ft - now
                            if wait > 0:
                                time.sleep(min(wait, 1.0))
                            elif wait < -0.25:
                                self.dropped += 1
                                continue
                        w, h = self._size_for(vf)
                        rgb = vf.reformat(width=w, height=h, format="rgb24", interpolation="BILINEAR")
                        pl = rgb.planes[0]
                        self.on_frame(bytes(pl), w, h, pl.line_size)
                        self.frames += 1
                        self.fps_hist.append(time.monotonic())
                        if time.monotonic() - last_info > 1.0:
                            last_info = time.monotonic()
                            fps = None
                            if len(self.fps_hist) > 2:
                                fps = (len(self.fps_hist) - 1) / (self.fps_hist[-1] - self.fps_hist[0])
                            self.on_info({"width": frame.width, "height": frame.height,
                                          "interlaced": interlaced, "render_fps": fps,
                                          "dropped": self.dropped, "pix_fmt": frame.format.name,
                                          "audio_backend": self.audio.backend if self.audio else None})
        except Exception as exc:
            if not self.stop_ev.is_set():
                self.on_error("Decodificação interrompida: %s" % exc)
        finally:
            try:
                container.close()
            except Exception:
                pass
            if self.audio:
                self.audio.stop()


def decode_thumbnail(ts_bytes, width=320):
    """Decodifica o primeiro quadro de vídeo de um trecho de TS. Retorna (rgb, w, h, stride) ou None."""
    if not HAVE_AV:
        return None
    import io
    try:
        c = av.open(io.BytesIO(ts_bytes), format="mpegts",
                    options={"probesize": str(len(ts_bytes)), "analyzeduration": "3000000"})
    except Exception:
        return None
    try:
        if not c.streams.video:
            return None
        vs = c.streams.video[0]
        for packet in c.demux(vs):
            try:
                frames = packet.decode()
            except Exception:
                continue
            for f in frames:
                h = int(round(f.height * width / max(1, f.width))) & ~1
                rgb = f.reformat(width=width, height=max(2, h), format="rgb24")
                pl = rgb.planes[0]
                return bytes(pl), width, max(2, h), pl.line_size, (f.width, f.height)
    except Exception:
        return None
    finally:
        c.close()
    return None
