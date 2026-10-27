"""Saída de áudio PCM s16le sem dependências: PulseAudio/PipeWire via ctypes, ou pw-cat/pacat/aplay."""

import ctypes
import ctypes.util
import shutil
import subprocess


class _SampleSpec(ctypes.Structure):
    _fields_ = [("format", ctypes.c_int), ("rate", ctypes.c_uint32), ("channels", ctypes.c_uint8)]


class _BufferAttr(ctypes.Structure):
    _fields_ = [("maxlength", ctypes.c_uint32), ("tlength", ctypes.c_uint32),
                ("prebuf", ctypes.c_uint32), ("minreq", ctypes.c_uint32), ("fragsize", ctypes.c_uint32)]


PA_SAMPLE_S16LE = 3
PA_STREAM_PLAYBACK = 1


class PulseOut:
    def __init__(self, rate=48000, channels=2, latency_ms=300):
        name = ctypes.util.find_library("pulse-simple") or "libpulse-simple.so.0"
        self.lib = ctypes.CDLL(name)
        self.lib.pa_simple_new.restype = ctypes.c_void_p
        self.lib.pa_simple_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                             ctypes.POINTER(ctypes.c_int)]
        self.lib.pa_simple_free.argtypes = [ctypes.c_void_p]
        self.lib.pa_simple_flush.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        spec = _SampleSpec(PA_SAMPLE_S16LE, rate, channels)
        bps = rate * channels * 2
        tl = int(bps * latency_ms / 1000)
        attr = _BufferAttr(0xFFFFFFFF, tl, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
        err = ctypes.c_int(0)
        self.h = self.lib.pa_simple_new(None, b"PIDgeon", PA_STREAM_PLAYBACK, None, b"Monitor de audio",
                                        ctypes.byref(spec), None, ctypes.byref(attr), ctypes.byref(err))
        if not self.h:
            raise OSError("pa_simple_new falhou (erro %d)" % err.value)
        self.name = "PulseAudio/PipeWire"

    def write(self, data):
        err = ctypes.c_int(0)
        if self.lib.pa_simple_write(self.h, data, len(data), ctypes.byref(err)) < 0:
            raise OSError("pa_simple_write erro %d" % err.value)

    def close(self):
        if self.h:
            self.lib.pa_simple_free(self.h)
            self.h = None


class PipeOut:
    def __init__(self, rate=48000, channels=2):
        cmds = [
            ["pw-cat", "--playback", "--format", "s16", "--rate", str(rate), "--channels", str(channels), "-"],
            ["pacat", "--playback", "--format=s16le", "--rate=%d" % rate, "--channels=%d" % channels,
             "--latency-msec=300"],
            ["aplay", "-q", "-f", "S16_LE", "-c", str(channels), "-r", str(rate)],
        ]
        for c in cmds:
            if shutil.which(c[0]):
                self.p = subprocess.Popen(c, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.name = c[0]
                return
        raise OSError("nenhuma saída de áudio disponível (pulse, pw-cat, pacat, aplay)")

    def write(self, data):
        self.p.stdin.write(data)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.terminate()
        except Exception:
            pass


def open_audio(rate=48000, channels=2):
    try:
        return PulseOut(rate, channels)
    except Exception:
        return PipeOut(rate, channels)
