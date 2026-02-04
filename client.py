import asyncio
import os
import json
import queue
import sys
import numpy as np
import websockets
import sphn
import sounddevice as sd
from collections import deque

SAMPLE_RATE = 24000
FRAME_SIZE = 480  # 20ms frames for better responsiveness (24000 / 50)
CHANNELS = 1

# Queues for audio data with max size to prevent buildup
MAX_QUEUE_SIZE = 10  # Max ~200ms of audio buffered
mic_queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
speaker_buffer = deque(maxlen=100000)  # Use deque for efficient speaker buffering
speaker_lock = asyncio.Lock()

# Statistics
stats = {"mic_overflows": 0, "speaker_underruns": 0, "queue_size": 0}

# Control flags
restart_requested = False
current_line_buffer = []  # Buffer for current line of text


def audio_callback_in(indata, frames, time, status):
    """Callback for microphone input."""
    if status:
        if status.input_overflow:
            stats["mic_overflows"] += 1

    # indata is (frames, channels), convert to mono if needed
    mono = indata[:, 0] if indata.shape[1] > 1 else indata.flatten()

    try:
        mic_queue.put_nowait(mono.copy())
    except queue.Full:
        # Drop old frames if queue is full (prevents latency buildup)
        try:
            mic_queue.get_nowait()
            mic_queue.put_nowait(mono.copy())
        except:
            pass


def audio_callback_out(outdata, frames, time, status):
    """Callback for speaker output."""
    if status:
        if status.output_underflow:
            stats["speaker_underruns"] += 1

    # Pull from speaker buffer
    if len(speaker_buffer) >= frames:
        for i in range(frames):
            outdata[i, 0] = speaker_buffer.popleft()
    else:
        # Not enough data - output what we have + silence
        available = len(speaker_buffer)
        for i in range(available):
            outdata[i, 0] = speaker_buffer.popleft()
        outdata[available:, 0] = 0
        if available < frames // 2:  # Only log if significant underrun
            stats["speaker_underruns"] += 1

    stats["queue_size"] = len(speaker_buffer)


async def stream_conversation():
    """Real-time streaming conversation with PersonaPlex."""
    global restart_requested, current_line_buffer

    uri = "wss://model-lqzgyvow.api.baseten.co/environments/production/websocket"

    api_key = os.environ.get("BASETEN_API_KEY")
    if not api_key:
        print("ERROR: BASETEN_API_KEY environment variable not set")
        return

    print(f"Connecting to: {uri}")
    print(f"Sample rate: {SAMPLE_RATE} Hz")
    print(
        f"Frame size: {FRAME_SIZE} samples ({FRAME_SIZE / SAMPLE_RATE * 1000:.1f} ms)"
    )
    print(
        f"Max queue size: {MAX_QUEUE_SIZE} frames ({MAX_QUEUE_SIZE * FRAME_SIZE / SAMPLE_RATE * 1000:.1f} ms)"
    )

    # List available audio devices
    print("\nAvailable audio devices:")
    devices = sd.query_devices()
    print(devices)

    # Try to find best input/output devices
    default_input = sd.default.device[0]
    default_output = sd.default.device[1]
    print(f"\nUsing input device: {default_input}")
    print(f"Using output device: {default_output}")

    try:
        # Start audio streams with smaller blocksize for better responsiveness
        input_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            callback=audio_callback_in,
            blocksize=FRAME_SIZE,
            dtype=np.float32,
            device=default_input,
        )
        output_stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            callback=audio_callback_out,
            blocksize=FRAME_SIZE,
            dtype=np.float32,
            device=default_output,
        )

        input_stream.start()
        output_stream.start()
        print("\n🎤 Microphone started")
        print("🔊 Speakers started")
        print("\n💡 Type 'restart' to restart the conversation, 'quit' to exit")

        async with websockets.connect(
            uri,
            additional_headers={"Authorization": f"Api-Key {api_key}"},
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            print("\n📡 WebSocket connected!")
            
            # Main conversation loop - can restart without reconnecting
            session_num = 1
            while True:
                restart_requested = False
                print(f"\n{'='*60}")
                print(f"Starting conversation session #{session_num}")
                print(f"{'='*60}\n")
                
                # Clear audio buffers
                while not mic_queue.empty():
                    try:
                        mic_queue.get_nowait()
                    except:
                        break
                speaker_buffer.clear()
                
                # Reset conversation tracking
                current_line_buffer = []
                print("\n" + "="*60)
                print("Start speaking... AI responses will appear below:")
                print("="*60)
                
                # Recreate opus codecs for new session
                opus_writer = sphn.OpusStreamWriter(SAMPLE_RATE)
                opus_reader = sphn.OpusStreamReader(SAMPLE_RATE)
                
                # Send config
                config = {
                    "voice_prompt": "NATF0.pt",
                    "text_prompt": "You are a helpful assistant.",
                    "seed": -1,
                }
                print("Sending config...")
                await ws.send(json.dumps(config))

                # Wait for handshake
                print("Waiting for handshake...")
                msg = await ws.recv()

                if isinstance(msg, bytes) and len(msg) > 0:
                    if msg[0] == 0x00:
                        print("✓ Handshake received! Ready to talk!\n")
                    else:
                        print(f"⚠️  Unexpected first byte: 0x{msg[0]:02x}")
                        break

                # Stats monitoring
                last_stats_time = asyncio.get_event_loop().time()

                async def send_audio():
                    """Send microphone audio to server."""
                    global restart_requested, current_line_buffer
                    accumulated = np.array([], dtype=np.float32)
                    frame_count = 0

                    while not restart_requested:
                        try:
                            # Get all available audio from microphone queue
                            while not mic_queue.empty():
                                chunk = mic_queue.get_nowait()
                                accumulated = np.concatenate([accumulated, chunk])

                            # Send full frames
                            while len(accumulated) >= FRAME_SIZE:
                                frame = accumulated[:FRAME_SIZE]
                                accumulated = accumulated[FRAME_SIZE:]

                                # Encode and send
                                opus_writer.append_pcm(frame)
                                data = opus_writer.read_bytes()
                                if len(data) > 0:
                                    await ws.send(bytes([0x01]) + data)
                                    frame_count += 1

                            await asyncio.sleep(0.01)  # 10ms loop for responsiveness

                        except Exception as e:
                            print(f"\n❌ Send error: {e}")
                            break

                async def receive_responses():
                    """Receive and play server responses."""
                    global restart_requested, current_speaker, current_line_buffer
                    nonlocal last_stats_time

                    try:
                        async for message in ws:
                            if restart_requested:
                                break
                                
                            if not isinstance(message, bytes) or len(message) == 0:
                                continue

                            msg_type = message[0]
                            payload = message[1:]

                            if msg_type == 0x00:  # Handshake (for restart)
                                print("\n✓ Restart handshake received! Session restarted!\n")
                                
                            elif msg_type == 0x01:  # Audio
                                opus_reader.append_bytes(payload)
                                pcm = opus_reader.read_pcm()
                                if pcm.shape[-1] > 0:
                                    # Check if AI is speaking (has audio output)
                                    volume = np.abs(pcm).mean()
                                    if volume > 0.01 and current_speaker != "AI":
                                        # Switch to AI speaker
                                        if current_line_buffer:
                                            print(''.join(current_line_buffer))
                                            current_line_buffer = []
                                        current_speaker = "AI"
                                        print("\n🤖 AI: ", end='', flush=True)
                                    
                                    # Add to speaker buffer
                                    # Clear buffer if it's getting too full (prevents latency)
                                    if len(speaker_buffer) > SAMPLE_RATE * 2:  # >2 seconds
                                        speaker_buffer.clear()

                                    speaker_buffer.extend(pcm)

                            elif msg_type == 0x02:  # Text
                                # Display transcript on same line
                                text = payload.decode("utf-8")
                                if text.strip():
                                    # Detect speaker changes based on text patterns or timing
                                    # For now, assume text belongs to current speaker
                                    if current_speaker is None:
                                        current_speaker = "AI"
                                        print("🤖 AI: ", end='', flush=True)
                                    
                                    current_line_buffer.append(text)
                                    print(text, end='', flush=True)

                    except Exception as e:
                        if not restart_requested:
                            print(f"\n❌ Receive error: {e}")
                
                async def monitor_input():
                    """Monitor stdin for restart/quit commands."""
                    global restart_requested
                    
                    loop = asyncio.get_event_loop()
                    reader = asyncio.StreamReader()
                    protocol = asyncio.StreamReaderProtocol(reader)
                    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
                    
                    while not restart_requested:
                        try:
                            # Read a line with timeout
                            line = await asyncio.wait_for(reader.readline(), timeout=0.5)
                            command = line.decode().strip().lower()
                            
                            if command == 'restart':
                                print("\n\n🔄 Restart requested! Restarting conversation...")
                                restart_requested = True
                                # Send restart control message: 0x03 (control) + 0x03 (restart)
                                await ws.send(bytes([0x03, 0x03]))
                                return True  # Signal to continue with restart
                            elif command == 'quit':
                                print("\n\n👋 Quit requested!")
                                restart_requested = True
                                return False  # Signal to quit entirely
                            elif command:
                                print(f"Unknown command: '{command}'. Use 'restart' or 'quit'")
                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            print(f"\n⚠️  Input error: {e}")
                            break
                    
                    return True  # Signal to continue with restart

                # Run send, receive, and input monitor concurrently
                continue_session = await asyncio.gather(
                    send_audio(),
                    receive_responses(),
                    monitor_input(),
                )
                
                # Check if we should quit or restart
                if continue_session[-1] is False:
                    print("\nExiting...")
                    break
                
                if not restart_requested:
                    break
                
                session_num += 1
                await asyncio.sleep(0.5)  # Brief pause before restart

    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped by user")
    except Exception as e:
        print(f"\n❌ Error: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
    finally:
        # Stop audio streams
        if "input_stream" in locals():
            input_stream.stop()
            input_stream.close()
        if "output_stream" in locals():
            output_stream.stop()
            output_stream.close()
        print("\n🔇 Audio streams closed")


if __name__ == "__main__":
    print("=" * 60)
    print("PersonaPlex Voice Chat Client")
    print("=" * 60)
    print("\nPress Ctrl+C to stop\n")

    try:
        asyncio.run(stream_conversation())
    except KeyboardInterrupt:
        print("\n\n👋 Goodbye!")
