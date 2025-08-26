import os
import logging
import queue
import time
from typing import Generator, Optional, Dict, Tuple, List
import subprocess, threading, shutil  # yea ffmpeg helpers & threads 

"""tiny twitch -> old formats bridge (kept casual)"""

from flask import Flask, Response, request, abort, send_from_directory, jsonify

try:
	from streamlink import Streamlink  # type: ignore
except ImportError: 
	Streamlink = None  # type: ignore


logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
log = logging.getLogger("twitchcompat")

app = Flask(__name__)

ERR_MSG = "Stream not playing, not found, or your internet failed"
# remember stuff for old browsers with long connection time
BUFFER_SECONDS = int(os.environ.get("BUFFER_SECONDS", "15"))
# HLS config
HLS_TIME = int(os.environ.get("HLS_TIME", "6"))            # target segment duration
HLS_LIST_SIZE = int(os.environ.get("HLS_LIST_SIZE", "6"))   # playlist segment count
HLS_WORKDIR = os.environ.get("HLS_WORKDIR", "/tmp/tc_hls")  # segment tmp dir
HLS_IDLE_TIMEOUT = int(os.environ.get("HLS_IDLE_TIMEOUT", "60"))


class _SharedTranscoder:
	def __init__(self, channel: str, quality: str, container: str):
		self.channel = channel
		self.quality = quality
		self.container = container  # mp4 / mp3
		self.lock = threading.Lock()
		self.subscribers: List[queue.Queue] = []
		self.buffer: List[Tuple[float, bytes]] = []  # (timestamp, chunk)
		self.last_sub_time = time.time()
		self.closed = False
		self.proc: Optional[subprocess.Popen] = None
		self.source_fd = open_stream_fd(channel, quality)
		if not self.source_fd:
			self.closed = True
			return
		self._start_ffmpeg()

	def _key(self):
		return (self.channel, self.quality, self.container)

	def _cmd(self):
		if self.container == 'mp3':
			return ["ffmpeg","-hide_banner","-loglevel","error","-i","pipe:0","-vn","-c:a","libmp3lame","-b:a","96k","-f","mp3","pipe:1"]
		# mp4 (low-latency settings)
		return [
			"ffmpeg","-hide_banner","-loglevel","error",
			"-analyzeduration","1000000","-probesize","64k","-fflags","nobuffer",
			"-i","pipe:0",
			"-vcodec","libx264","-preset","veryfast","-tune","zerolatency",
			"-profile:v","baseline","-level","3.0","-pix_fmt","yuv420p",
			"-g","48","-keyint_min","48","-force_key_frames","expr:gte(t,n_forced*2)",
			"-acodec","aac","-strict","-2","-b:a","128k",
			"-movflags","+frag_keyframe+empty_moov+default_base_moof+faststart","-flush_packets","1",
			"-f","mp4","pipe:1"
		]

	def _start_ffmpeg(self):
		if shutil.which("ffmpeg") is None:
			log.error("ffmpeg missing, cannot start shared transcoder %s", self.container)
			self.closed = True
			return
		try:
			self.proc = subprocess.Popen(self._cmd(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
		except Exception as e:
			log.error("failed to start shared ffmpeg %s: %s", self.container, e)
			self.closed = True
			return
		threading.Thread(target=self._feeder, daemon=True).start()
		threading.Thread(target=self._reader, daemon=True).start()
		threading.Thread(target=self._stderr_logger, daemon=True).start()
		threading.Thread(target=self._reaper, daemon=True).start()

	def _feeder(self):
		fd = self.source_fd
		try:
			while not self.closed:
				if not fd or (self.proc and self.proc.poll() is not None):
					break
				chunk = fd.read(188 * 32)
				if not chunk:
					break
				try:
					if self.proc and self.proc.stdin:
						self.proc.stdin.write(chunk)
				except Exception:
					break
		finally:
			try:
				if self.proc and self.proc.stdin:
					self.proc.stdin.close()
			except Exception: pass
			try:
				if fd:
					fd.close()
			except Exception: pass

	def _reader(self):
		if not self.proc or not self.proc.stdout:
			return
		try:
			while not self.closed:
				if self.proc.poll() is not None and (hasattr(self.proc.stdout, 'peek') and self.proc.stdout.peek(1) == b''):
					break
				out = self.proc.stdout.read(32 * 1024)
				if not out:
					if self.proc.poll() is not None:
						break
					continue
				now = time.time()
				with self.lock:
					self.buffer.append((now, out))
					cutoff = now - BUFFER_SECONDS
					while self.buffer and self.buffer[0][0] < cutoff:
						self.buffer.pop(0)
					for q in list(self.subscribers):
						try:
							if q.qsize() > 60:
								continue
							q.put_nowait(out)
						except Exception:
							pass
		finally:
			self.closed = True
			self._broadcast_end()
			log.info("shared transcoder ended %s/%s/%s", self.channel, self.quality, self.container)
			# remove self from global map to avoid leaks
			try:
				with _TRANSCODER_LOCK:
					_TRANSCODERS.pop(self._key(), None)
			except Exception:
				pass

	def _stderr_logger(self):
		if not self.proc or not self.proc.stderr:
			return
		try:
			for line in iter(self.proc.stderr.readline, b''):
				if not line:
					break
				log.debug("ffmpeg(%s %s): %s", self.container, self.channel, line.decode('utf-8','ignore').strip())
		except Exception:
			pass
		finally:
			try:
				if self.proc and self.proc.stderr:
					self.proc.stderr.close()
			except Exception:
				pass

	def _broadcast_end(self):
		with self.lock:
			for q in self.subscribers:
				try: q.put_nowait(None)
				except Exception: pass
			self.subscribers.clear()

	def _reaper(self):
		while not self.closed:
			time.sleep(2)
			with self.lock:
				idle = (not self.subscribers) and (time.time() - self.last_sub_time > BUFFER_SECONDS)
			if idle:
				self.closed = True
				try:
					if self.proc:
						self.proc.kill()
				except Exception: pass
				# ensure we remove from global map
				try:
					with _TRANSCODER_LOCK:
						_TRANSCODERS.pop(self._key(), None)
				except Exception:
					pass
				break

	def subscribe(self) -> Generator[bytes, None, None]:
		if self.closed:
			return (b'' for _ in [])
		q: queue.Queue = queue.Queue()
		with self.lock:
			snapshot = list(self.buffer)
			self.subscribers.append(q)
			self.last_sub_time = time.time()
		def gen():
			for _, chunk in snapshot:
				yield chunk
			try:
				while True:
					item = q.get()
					if item is None:
						break
					yield item
			finally:
				with self.lock:
					if q in self.subscribers:
						self.subscribers.remove(q)
					self.last_sub_time = time.time()
		return gen()


class _HLSManager:
	def __init__(self, channel: str, quality: str):
		self.channel = channel
		self.quality = quality
		self.dir = os.path.join(HLS_WORKDIR, f"{channel}_{quality}")
		self.proc: Optional[subprocess.Popen] = None
		self.source_fd = open_stream_fd(channel, quality)
		self.closed = False
		self.last_touch = time.time()
		if not self.source_fd:
			self.closed = True
			return
		os.makedirs(self.dir, exist_ok=True)
		self._start_ffmpeg(copy_first=True)
		threading.Thread(target=self._feeder, daemon=True).start()
		threading.Thread(target=self._stderr_logger, daemon=True).start()
		threading.Thread(target=self._reaper, daemon=True).start()

	def _playlist(self):
		return os.path.join(self.dir, 'index.m3u8')

	def _start_ffmpeg(self, copy_first: bool):
		if shutil.which('ffmpeg') is None:
			log.error('ffmpeg missing, cannot start hls segmenter')
			self.closed = True
			return
		cmd = ['ffmpeg','-hide_banner','-loglevel','error','-i','pipe:0']
		if copy_first:
			cmd += ['-c:v','copy','-c:a','copy']
		else:
			cmd += ['-c:v','libx264','-preset','veryfast','-tune','zerolatency','-c:a','aac','-b:a','128k']
		cmd += [
			'-f','hls',
			'-hls_time', str(HLS_TIME),
			'-hls_list_size', str(HLS_LIST_SIZE),
			'-hls_flags','delete_segments+omit_endlist',
			'-hls_segment_filename', os.path.join(self.dir, 'seg_%05d.ts'),
			self._playlist()
		]
		try:
			self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
		except Exception as e:
			if copy_first:
				log.warning('hls copy-first failed: %s, retrying with transcode', e)
				self._start_ffmpeg(copy_first=False)
				return
			log.error('failed to start hls ffmpeg: %s', e)
			self.closed = True

	def _feeder(self):
		fd = self.source_fd
		try:
			while not self.closed:
				if not fd or (self.proc and self.proc.poll() is not None):
					break
				chunk = fd.read(188 * 32)
				if not chunk:
					break
				try:
					if self.proc and self.proc.stdin:
						self.proc.stdin.write(chunk)
				except Exception:
					break
		finally:
			try:
				if self.proc and self.proc.stdin:
					self.proc.stdin.close()
			except Exception: pass
			try:
				if fd: fd.close()
			except Exception: pass

	def _stderr_logger(self):
		if not self.proc or not self.proc.stderr:
			return
		try:
			for line in iter(self.proc.stderr.readline, b''):
				if not line: break
				log.debug('ffmpeg(hls %s): %s', self.channel, line.decode('utf-8','ignore').strip())
		except Exception: pass
		finally:
			try:
				if self.proc and self.proc.stderr: self.proc.stderr.close()
			except Exception: pass

	def _reaper(self):  #
		while not self.closed:
			time.sleep(5)
			if time.time() - self.last_touch > HLS_IDLE_TIMEOUT:
				self.close()
				break

	def touch(self):
		self.last_touch = time.time()

	def close(self): 
		if self.closed: return
		self.closed = True
		try:
			if self.proc: self.proc.kill()
		except Exception: pass
		# remove from global registry so it can be GC'd
		try:
			with _HLS_LOCK:
				_HLS_MANAGERS.pop((self.channel, self.quality), None)
		except Exception:
			pass
		# try to remove the directory if it's empty to avoid buildup
		try:
			if os.path.isdir(self.dir):
				try:
					# remove any leftover small files that are not active segments (best-effort)
					entries = os.listdir(self.dir)
					if not entries:
						os.rmdir(self.dir)
				except Exception:
					# best-effort only
					pass
		except Exception:
			pass

	def playlist(self) -> Optional[str]:  
		self.touch()
		p = self._playlist()
		if not os.path.exists(p):
			return None
		try:
			with open(p,'r',encoding='utf-8',errors='ignore') as f:
				return f.read()
		except Exception as e:
			log.warning('read playlist error: %s', e)
			return None

	def segment(self, name: str) -> Optional[bytes]:  
		self.touch()
		p = os.path.join(self.dir, name)
		if not os.path.isfile(p):
			return None
		try:
			with open(p,'rb') as f:
				return f.read()
		except Exception:
			return None

_HLS_MANAGERS: Dict[Tuple[str,str], _HLSManager] = {}
_HLS_LOCK = threading.Lock()

def get_hls_manager(channel: str, quality: str) -> _HLSManager:
	key = (channel, quality)
	with _HLS_LOCK:
		mgr = _HLS_MANAGERS.get(key)
		if mgr:
			# if found but closed, remove it so we create a fresh one
			if not mgr.closed:
				return mgr
			_HLS_MANAGERS.pop(key, None)
		mgr = _HLSManager(channel, quality)
		# only register if started successfully
		if mgr.closed:
			return mgr
		_HLS_MANAGERS[key] = mgr
		return mgr


_TRANSCODERS: Dict[Tuple[str,str,str], _SharedTranscoder] = {}
_TRANSCODER_LOCK = threading.Lock()

def get_shared_transcoder(channel: str, quality: str, container: str) -> _SharedTranscoder:
	key = (channel, quality, container)
	with _TRANSCODER_LOCK:
		tr = _TRANSCODERS.get(key)
		if tr:
			# if found but closed remove and make a new one
			if not tr.closed:
				return tr
			_TRANSCODERS.pop(key, None)
		tr = _SharedTranscoder(channel, quality, container)
		if tr.closed:
			return tr
		_TRANSCODERS[key] = tr
		return tr


def new_session():
	if Streamlink is None:
		abort(500, "streamlink not installed")
	session = Streamlink()
	return session


def open_stream_fd(channel: str, quality: str) -> Optional[object]:
	session = new_session()
	url = f"https://twitch.tv/{channel}"
	try:
		streams = session.streams(url)
	except Exception as e:
		log.warning("couldnt grab streams for %s: %s", channel, e)
		return None

	if not streams:
		return None

	chosen = streams.get(quality) or streams.get('best')  # fallback for dummies
	if not chosen:
		return None

	try:
		fd = chosen.open()
	except Exception as e:
		log.warning("cant open stream %s @ %s: %s", channel, quality, e)
		return None
	return fd


def ts_chunks(fd, chunk_size: int = 188 * 7) -> Generator[bytes, None, None]:
	try:
		while True:
			data = fd.read(chunk_size)
			if not data:
				break
			yield data
	finally:
		try:
			fd.close()
		except Exception:
			pass
		log.info("disconnected from stream (fd closed)")


def transmux_ts_to_mp4(fd) -> Optional[Generator[bytes, None, None]]:
	if shutil.which("ffmpeg") is None:
		log.error("ffmpeg missing, cannot transmux to mp4")
		return None
	try:
		proc = subprocess.Popen([
			"ffmpeg","-hide_banner","-loglevel","error",
			"-analyzeduration","1000000","-probesize","64k","-fflags","nobuffer",
			"-i","pipe:0",
			"-vcodec","libx264","-preset","veryfast","-tune","zerolatency",
			"-profile:v","baseline","-level","3.0","-pix_fmt","yuv420p",
			"-g","48","-keyint_min","48","-force_key_frames","expr:gte(t,n_forced*2)",
			"-acodec","aac","-strict","-2","-b:a","128k",
			"-movflags","+frag_keyframe+empty_moov+default_base_moof+faststart","-flush_packets","1",
			"-f","mp4","pipe:1"
		], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
	except Exception as e:
		log.error("failed to start ffmpeg: %s", e)
		return None

	def feeder():
		try:
			while True:
				if proc.poll() is not None:
					break
				chunk = fd.read(188 * 32)
				if not chunk:
					break
				try:
					proc.stdin.write(chunk)
				except (BrokenPipeError, ValueError):
					break
		finally:
			for x in (proc.stdin,):
				try: x.close()
				except Exception: pass
			try: fd.close()
			except Exception: pass

	def stderr_logger():
		try:
			for line in iter(proc.stderr.readline, b''):
				if not line:
					break
				log.warning("ffmpeg: %s", line.decode('utf-8', 'ignore').strip())
		except Exception:
			pass
		finally:
			try:
				proc.stderr.close()
			except Exception:
				pass

	threading.Thread(target=feeder, daemon=True).start()
	threading.Thread(target=stderr_logger, daemon=True).start()

	def gen():
		try:
			while True:
				if proc.poll() is not None and proc.stdout.peek(1) == b'' if hasattr(proc.stdout, "peek") else False:
					break
				out = proc.stdout.read(32 * 1024)
				if not out:
					if proc.poll() is not None:
						break
					continue
				yield out
		finally:
			try:
				proc.kill()
			except Exception:
				pass
			log.info("mp4 transmux ended")
	return gen()



def extract_wav_audio(fd) -> Optional[Generator[bytes, None, None]]:
	"""
	Extracts / decodes audio only to WAV (PCM) for legacy clients.
	"""
	if shutil.which("ffmpeg") is None:
		log.error("ffmpeg missing, cannot extract wav")
		return None
	try:
		proc = subprocess.Popen(
			[
				"ffmpeg",
				"-hide_banner",
				"-loglevel", "error",
				"-i", "pipe:0",
				"-vn",
				"-c:a", "pcm_s16le",
				"-ar", "44100",
				"-ac", "2",
				"-f", "wav",
				"pipe:1",
			],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			bufsize=0
		)
	except Exception as e:
		log.error("failed to start ffmpeg (wav): %s", e)
		return None

	def feeder():
		try:
			while True:
				if proc.poll() is not None:
					break
				chunk = fd.read(188 * 24)
				if not chunk:
					break
				try:
					proc.stdin.write(chunk)
				except (BrokenPipeError, ValueError):
					break
		finally:
			for x in (proc.stdin,):
				try: x.close()
				except Exception: pass
			try: fd.close()
			except Exception: pass

	def stderr_logger():
		try:
			for line in iter(proc.stderr.readline, b''):
				if not line:
					break
				log.warning("ffmpeg(wav): %s", line.decode('utf-8', 'ignore').strip())
		except Exception:
			pass
		finally:
			try: proc.stderr.close()
			except Exception: pass

	threading.Thread(target=feeder, daemon=True).start()
	threading.Thread(target=stderr_logger, daemon=True).start()

	def gen():
		try:
			while True:
				if proc.poll() is not None and (hasattr(proc.stdout, "peek") and proc.stdout.peek(1) == b''):
					break
				out = proc.stdout.read(16 * 1024)
				if not out:
					if proc.poll() is not None:
						break
					continue
				yield out
		finally:
			try: proc.kill()
			except Exception: pass
			log.info("wav extraction ended")
	return gen()


def extract_mp3_audio(fd) -> Optional[Generator[bytes, None, None]]:
	if shutil.which("ffmpeg") is None:
		log.error("ffmpeg missing, cannot extract mp3")
		return None
	try:
		proc = subprocess.Popen(
			[
				"ffmpeg",
				"-hide_banner",
				"-loglevel", "error",
				"-i", "pipe:0",
				"-vn",
				"-c:a", "libmp3lame",
				"-b:a", "96k",
				"-f", "mp3",
				"pipe:1",
			],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			bufsize=0
		)
	except Exception as e:
		log.error("failed to start ffmpeg (mp3): %s", e)
		return None

	def feeder():
		try:
			while True:
				if proc.poll() is not None:
					break
				chunk = fd.read(188 * 24)
				if not chunk:
					break
				try:
					proc.stdin.write(chunk)
				except (BrokenPipeError, ValueError):
					break
		finally:
			for x in (proc.stdin,):
				try: x.close()
				except Exception: pass
			try: fd.close()
			except Exception: pass

	def stderr_logger():
		try:
			for line in iter(proc.stderr.readline, b''):
				if not line:
					break
				log.warning("ffmpeg(mp3): %s", line.decode('utf-8', 'ignore').strip())
		except Exception:
			pass
		finally:
			try: proc.stderr.close()
			except Exception: pass

	threading.Thread(target=feeder, daemon=True).start()
	threading.Thread(target=stderr_logger, daemon=True).start()

	def gen():
		try:
			while True:
				if proc.poll() is not None and (hasattr(proc.stdout, "peek") and proc.stdout.peek(1) == b''):
					break
				out = proc.stdout.read(8 * 1024)
				if not out:
					if proc.poll() is not None:
						break
					continue
				yield out
		finally:
			try: proc.kill()
			except Exception: pass
			log.info("mp3 extraction ended")
	return gen()


def single_snapshot_jpeg(fd) -> Optional[bytes]:
	if shutil.which("ffmpeg") is None:
		log.error("ffmpeg missing, cannot snapshot")
		return None
	try:
		proc = subprocess.Popen(
			[
				"ffmpeg","-hide_banner","-loglevel","error","-i","pipe:0",
				"-vframes","1","-vf","scale=640:-2,format=yuv420p",
				"-q:v","4","-f","image2","pipe:1"
			],
			stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
		)
	except Exception as e:
		log.error("failed starting ffmpeg (snapshot): %s", e)
		return None
	try:
		while True:
			chunk = fd.read(188 * 64)
			if not chunk:
				break
			try:
				proc.stdin.write(chunk)
			except Exception:
				break
			if proc.stdout.peek(1) if hasattr(proc.stdout, "peek") else True:
				pass
			if proc.poll() is not None:
				break
		data = proc.stdout.read()
		return data if data else None
	finally:
		for x in (proc.stdin, proc.stdout, proc.stderr):
			try: x.close()
			except Exception: pass
		try: fd.close()
		except Exception: pass
		try: proc.kill()
		except Exception: pass
	log.info("snapshot generated")
	return None


def transcode_ts_to_mjpeg(fd) -> Optional[Generator[bytes, None, None]]:
	if shutil.which("ffmpeg") is None:
		log.error("ffmpeg missing, cannot mjpeg")
		return None
	try:
		proc = subprocess.Popen(
			[
				"ffmpeg","-hide_banner","-loglevel","error","-i","pipe:0",
				"-r","5","-vf","scale=426:-2,format=yuvj420p",
				"-q:v","7","-f","mpjpeg","pipe:1"
			],
			stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
		)
	except Exception as e:
		log.error("failed starting ffmpeg (mjpeg): %s", e)
		return None

	def feeder():
		try:
			while True:
				if proc.poll() is not None:
					break
				chunk = fd.read(188 * 32)
				if not chunk:
					break
				try: proc.stdin.write(chunk)
				except (BrokenPipeError, ValueError): break
		finally:
			for x in (proc.stdin,):
				try: x.close()
				except Exception: pass
			try: fd.close()
			except Exception: pass

	def stderr_logger():
		try:
			for line in iter(proc.stderr.readline, b''):
				if not line: break
				log.warning("ffmpeg(mjpeg): %s", line.decode('utf-8','ignore').strip())
		except Exception: pass
		finally:
			try: proc.stderr.close()
			except Exception: pass

	threading.Thread(target=feeder, daemon=True).start()
	threading.Thread(target=stderr_logger, daemon=True).start()

	def gen():
		try:
			while True:
				if proc.poll() is not None and (hasattr(proc.stdout, "peek") and proc.stdout.peek(1) == b''):
					break
				out = proc.stdout.read(16 * 1024)
				if not out:
					if proc.poll() is not None:
						break
					continue
				yield out
		finally:
			try: proc.kill()
			except Exception: pass
			log.info("mjpeg transcode ended")
	return gen()


def _no_cache_headers(resp: Response) -> Response:
	resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
	resp.headers['Pragma'] = 'no-cache'
	resp.headers['Expires'] = '0'
	resp.headers['Accept-Ranges'] = 'none'
	return resp

def _build_stream_response(gen: Optional[Generator[bytes, None, None]], mimetype: str):
	if not gen:
		abort(404, ERR_MSG)
	if request.method == 'HEAD':
		resp = Response(b'', mimetype=mimetype)
		return _no_cache_headers(resp)
	resp = Response(gen, mimetype=mimetype, direct_passthrough=True)
	return _no_cache_headers(resp)


@app.route('/')
def index():
	resp = send_from_directory('.', 'index.html')
	return _no_cache_headers(resp)


@app.route('/health')
def health():
	return jsonify(status="ok"), 200


@app.errorhandler(404)
def not_found(e):
	if getattr(e, "description", "") == ERR_MSG:
		return ERR_MSG, 404, {"Content-Type": "text/plain; charset=utf-8"}
	return e, 404


@app.route('/stream/<channel>', methods=['GET', 'HEAD'])
def stream(channel: str):
	quality = request.args.get('quality', 'best')
	container = request.args.get('container', 'ts')
	log.info("client wants channel=%s quality=%s container=%s method=%s", channel, quality, container, request.method)
	fd = open_stream_fd(channel, quality)
	if not fd:
		abort(404, ERR_MSG)
	if container in ('mp4','mp3'):
		# shared transcoder path, close initial fd (each shared transcoder opens its own source)
		try: fd.close()
		except Exception: pass
		tr = get_shared_transcoder(channel, quality, container)
		if tr.closed:
			abort(404, ERR_MSG)
		mime = 'video/mp4' if container=='mp4' else 'audio/mpeg'
		return _build_stream_response(tr.subscribe(), mime)
	elif container == 'wav':
		return _build_stream_response(extract_wav_audio(fd), 'audio/wav')
	elif container == 'mjpeg':
		return _build_stream_response(transcode_ts_to_mjpeg(fd), 'multipart/x-mixed-replace; boundary=ffmpeg')
	elif container == 'ts':
		return _build_stream_response(ts_chunks(fd), 'video/mp2t')
	else:
		log.warning("unsupported container requested: %s", container)
		abort(400, "unsupported container")


@app.route('/mp4/<channel>.mp4', methods=['GET', 'HEAD'])
def legacy_mp4(channel: str):
	quality = request.args.get('quality', 'best')
	tr = get_shared_transcoder(channel, quality, 'mp4')
	if tr.closed:
		abort(404, ERR_MSG)
	return _build_stream_response(tr.subscribe(), 'video/mp4')

@app.route('/hls/<channel>.m3u8', methods=['GET'])
def hls_master(channel: str):
	# simple single-variant master playlist referencing media playlist
	quality = request.args.get('quality', 'best')
	variant_playlist = f"/hls/{channel}_{quality}.m3u8"
	content = "#EXTM3U\n" \
		"#EXT-X-VERSION:3\n" \
		f"#EXT-X-STREAM-INF:BANDWIDTH=800000,NAME=\"{quality}\"\n" \
		f"{variant_playlist}\n"
	resp = Response(content, mimetype='application/vnd.apple.mpegurl')
	return _no_cache_headers(resp)

@app.route('/hls/<channel>_<quality>.m3u8', methods=['GET'])
def hls_variant_playlist(channel: str, quality: str):
	# build / fetch dynamic media playlist from HLS manager and rewrite segment URIs
	mgr = get_hls_manager(channel, quality)
	if mgr.closed:
		abort(404, ERR_MSG)
	pl = mgr.playlist()
	if not pl:
		abort(404, ERR_MSG)
	# rewrite relative segment names to our segment fetch endpoint path: seg/<channel>_<quality>/<segment>
	lines_out: List[str] = []
	for line in pl.splitlines():
		if line and not line.startswith('#') and line.endswith('.ts'):
			line = f"seg/{channel}_{quality}/{line}"
		lines_out.append(line)
	content = "\n".join(lines_out) + "\n"
	resp = Response(content, mimetype='application/vnd.apple.mpegurl')
	return _no_cache_headers(resp)

@app.route('/hls/seg/<channel>_<quality>/<segment>', methods=['GET'])
def hls_segment(channel: str, quality: str, segment: str):
	mgr = get_hls_manager(channel, quality)
	if mgr.closed:
		abort(404, ERR_MSG)
	if not segment.startswith('seg_') or not segment.endswith('.ts'):
		abort(400, 'bad segment name')
	data = mgr.segment(segment)
	if not data:
		abort(404, ERR_MSG)
	resp = Response(data, mimetype='video/mp2t')
	return _no_cache_headers(resp)

@app.route('/ts/<channel>.ts', methods=['GET', 'HEAD'])
def legacy_ts(channel: str):
	quality = request.args.get('quality', 'best')
	fd = open_stream_fd(channel, quality)
	return _build_stream_response(ts_chunks(fd) if fd else None, 'video/mp2t')

@app.route('/wav/<channel>.wav', methods=['GET', 'HEAD'])
def legacy_wav(channel: str):
	quality = request.args.get('quality', 'best')
	fd = open_stream_fd(channel, quality)
	return _build_stream_response(extract_wav_audio(fd) if fd else None, 'audio/wav')

@app.route('/mp3/<channel>.mp3', methods=['GET', 'HEAD'])
def legacy_mp3(channel: str):
	quality = request.args.get('quality', 'best')
	tr = get_shared_transcoder(channel, quality, 'mp3')
	if tr.closed:
		abort(404, ERR_MSG)
	return _build_stream_response(tr.subscribe(), 'audio/mpeg')

@app.route('/snapshot/<channel>.jpg', methods=['GET'])
def legacy_snapshot(channel: str):
	quality = request.args.get('quality', 'best')
	fd = open_stream_fd(channel, quality)
	if not fd:
		abort(404, ERR_MSG)
	data = single_snapshot_jpeg(fd)
	if not data:
		abort(404, ERR_MSG)
	resp = Response(data, mimetype='image/jpeg')
	return _no_cache_headers(resp)

@app.route('/mjpeg/<channel>', methods=['GET', 'HEAD'])
def legacy_mjpeg(channel: str):
	quality = request.args.get('quality', 'best')
	fd = open_stream_fd(channel, quality)
	return _build_stream_response(transcode_ts_to_mjpeg(fd) if fd else None, 'multipart/x-mixed-replace; boundary=ffmpeg')


@app.after_request
def add_global_no_cache(resp):
	if request.path.startswith(('/stream/', '/mp4/', '/ts/', '/wav/', '/mp3/', '/mjpeg/', '/snapshot/', '/hls/')) or request.path == '/':
		resp = _no_cache_headers(resp)
	return resp


def main():
	host = os.environ.get('HOST', '0.0.0.0')
	port = int(os.environ.get('PORT', '8001'))
	log.info("Starting TwitchCompat on %s:%s", host, port)
	app.run(host=host, port=port, threaded=True)


if __name__ == '__main__':
	main()
