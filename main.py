import os
import logging
from typing import Generator, Optional
import subprocess, threading, shutil  # added for mp4 transmux

from flask import Flask, Response, request, abort, send_from_directory, jsonify

try:
	from streamlink import Streamlink
except ImportError: 
	Streamlink = None  


logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
log = logging.getLogger("twitchcompat")

app = Flask(__name__)

ERROR_MSG = "Stream not playing, not found, or your internet failed"  # unified text


def make_session() -> Streamlink:
	if Streamlink is None:
		abort(500, "streamlink not installed")
	session = Streamlink()
	return session


def open_the_stream(channel: str, quality: str) -> Optional[object]:
	session = make_session()
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


def stream_chunks(fd, chunk_size: int = 188 * 7) -> Generator[bytes, None, None]:
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
		proc = subprocess.Popen(
			[
				"ffmpeg",
				"-hide_banner",
				"-loglevel", "error",
				"-i", "pipe:0",
				"-c", "copy",
				# add aac bitstream filter so mp4 stops whining
				"-bsf:a", "aac_adtstoasc",
				"-movflags", "+frag_keyframe+empty_moov+default_base_moof",
				"-f", "mp4",
				"-reset_timestamps", "1",
				"pipe:1",
			],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,  # so we can peek at errors
			bufsize=0
		)
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
		# log ffmpeg complaint
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


def transcode_ts_to_avi(fd) -> Optional[Generator[bytes, None, None]]:
	if shutil.which("ffmpeg") is None:
		log.error("ffmpeg missing, cannot transcode to avi")
		return None
	try:
		proc = subprocess.Popen(
			[
				"ffmpeg",
				"-hide_banner",
				"-loglevel", "error",
				"-i", "pipe:0",
				"-r", "15",
				"-c:v", "cinepak",
				"-b:v", "500k",
				# pixel format for broadest decoder support
				"-pix_fmt", "yuv420p",
				# uncompressed PCM audio
				"-c:a", "pcm_s16le",
				"-ar", "44100",
				"-ac", "2",
				"-f", "avi",
				"pipe:1",
			],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			bufsize=0
		)
	except Exception as e:
		log.error("failed to start ffmpeg (avi): %s", e)
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
				log.warning("ffmpeg(avi): %s", line.decode('utf-8', 'ignore').strip())
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
				out = proc.stdout.read(32 * 1024)
				if not out:
					if proc.poll() is not None:
						break
					continue
				yield out
		finally:
			try: proc.kill()
			except Exception: pass
			log.info("avi transcode ended")
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


@app.route('/')
def index():
	return send_from_directory('.', 'index.html')


@app.route('/health')
def health():
	return jsonify(status="ok"), 200


@app.errorhandler(404)
def not_found(e):

	if getattr(e, "description", "") == ERROR_MSG:
		return ERROR_MSG, 404, {"Content-Type": "text/plain; charset=utf-8"}
	return e, 404


@app.route('/stream/<channel>')
def stream(channel: str):
	quality = request.args.get('quality', 'best')
	container = request.args.get('container', 'ts')
	log.info("client wants channel=%s quality=%s container=%s", channel, quality, container)
	fd = open_the_stream(channel, quality)
	if not fd:
		abort(404, ERROR_MSG)
	if container == 'mp4':
		gen = transmux_ts_to_mp4(fd)
		if not gen:
			abort(404, ERROR_MSG)
		return Response(gen, mimetype='video/mp4')
	elif container == 'avi':
		gen = transcode_ts_to_avi(fd)
		if not gen:
			abort(404, ERROR_MSG)
		return Response(gen, mimetype='video/x-msvideo')
	elif container == 'wav':
		gen = extract_wav_audio(fd)
		if not gen:
			abort(404, ERROR_MSG)
		return Response(gen, mimetype='audio/wav')
	elif container == 'ts':
		return Response(stream_chunks(fd), mimetype='video/mp2t')
	else:
		log.warning("unsupported container requested: %s", container)
		abort(400, "unsupported container")


def main():
	host = os.environ.get('HOST', '0.0.0.0')
	port = int(os.environ.get('PORT', '8001'))
	log.info("Starting TwitchCompat on %s:%s", host, port)
	app.run(host=host, port=port, threaded=True)


if __name__ == '__main__':
	main()

