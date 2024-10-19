# Cut every 5 seconds


import queue
import re
import sys
import time
import json
import os

from google.cloud import speech
from google.cloud import translate_v3
from google.cloud import pubsub_v1

import pyaudio

# Audio recording parameters
STREAMING_LIMIT = 240000  # 4 minutes
SAMPLE_RATE = 16000
CHUNK_SIZE = int(SAMPLE_RATE / 10)  # 100ms

TRANSLATION_BUFFER = 5
RECORDING_SECONDS = 5

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"

project_id = os.environ.get("PROJECT_ID")
pubsub_topic = os.environ.get("PUBSUB_TOPIC")

location = "global"

translator = translate_v3.TranslationServiceClient()

publisher_client = pubsub_v1.PublisherClient(publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True))
topic_name = "projects/{project_id}/topics/{topic}".format(
    project_id=project_id,
    topic=pubsub_topic
)

# stt_language_code = "th-TH"
stt_language_code = "en-US"

# translation_source_language_code = "th"
translation_source_language_code = "en-US"
# translation_target_language_code = "en-US"
translation_target_language_code = "th"


def get_current_time() -> int:
    """Return Current Time in MS.

    Returns:
        int: Current Time in MS.
    """

    return int(round(time.time() * 1000))


class ResumableMicrophoneStream:
    """Opens a recording stream as a generator yielding the audio chunks."""

    def __init__(
        self: object,
        rate: int,
        chunk_size: int,
    ) -> None:
        """Creates a resumable microphone stream.

        Args:
        self: The class instance.
        rate: The audio file's sampling rate.
        chunk_size: The audio file's chunk size.

        returns: None
        """
        self._rate = rate
        self.chunk_size = chunk_size
        self._num_channels = 1
        self._buff = queue.Queue()
        self.closed = True
        self.start_time = get_current_time()
        self.restart_counter = 0
        self.audio_input = []
        self.last_audio_input = []
        self.result_end_time = 0
        self.is_final_end_time = 0
        self.final_request_end_time = 0
        self.bridging_offset = 0
        self.last_transcript_was_final = False
        self.new_stream = True
        self._audio_interface = pyaudio.PyAudio()
        self._audio_stream = self._audio_interface.open(
            format=pyaudio.paInt16,
            channels=self._num_channels,
            rate=self._rate,
            input=True,
            frames_per_buffer=self.chunk_size,
            # Run the audio stream asynchronously to fill the buffer object.
            # This is necessary so that the input device's buffer doesn't
            # overflow while the calling thread makes network requests, etc.
            stream_callback=self._fill_buffer,
        )
        self.last_cut_time = time.time()
        self.chunk_buffer = queue.Queue()

    def __enter__(self: object) -> object:
        """Opens the stream.

        Args:
        self: The class instance.

        returns: None
        """
        self.closed = False
        return self

    def __exit__(
        self: object,
        type: object,
        value: object,
        traceback: object,
    ) -> object:
        """Closes the stream and releases resources.

        Args:
        self: The class instance.
        type: The exception type.
        value: The exception value.
        traceback: The exception traceback.

        returns: None
        """
        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        # Signal the generator to terminate so that the client's
        # streaming_recognize method will not block the process termination.
        self._buff.put(None)
        self._audio_interface.terminate()

    def _fill_buffer(
        self: object,
        in_data: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        """Continuously collect data from the audio stream, into the buffer.

        Args:
        self: The class instance.
        in_data: The audio data as a bytes object.
        args: Additional arguments.
        kwargs: Additional arguments.

        returns: None
        """
        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def generator(self: object) -> object:
        """Stream Audio from microphone to API and to local buffer

        Args:
            self: The class instance.

        returns:
            The data from the audio stream.
        """
        while not self.closed:
            if time.time() - self.last_cut_time >= RECORDING_SECONDS:
                # Keep the last 2 second of audio
                num_chunks_to_keep = int(2 * SAMPLE_RATE / CHUNK_SIZE)  # 2 second of chunks
                
                while self.chunk_buffer.qsize() > num_chunks_to_keep:
                    try:
                        self.chunk_buffer.get_nowait()
                    except queue.Empty:
                        break

                self.last_cut_time = time.time()
                self._buff.put(None)  # Signal the end of the current stream

            chunk = self._buff.get()
            if chunk is None:
                return
            data = [chunk]
            self.chunk_buffer.put(chunk)

            # Now consume whatever other data's still buffered.
            while True:
                try:
                    chunk = self._buff.get(block=False)
                    if chunk is None:
                        return
                    data.append(chunk)

                    self.chunk_buffer.put(chunk)
                except queue.Empty:
                    break

            yield b"".join(data)
            self.new_stream = True


def listen_print_loop(responses: object, stream: object) -> None:
    """Iterates through server responses and prints them.

    The responses passed is a generator that will block until a response
    is provided by the server.

    Each response may contain multiple results, and each result may contain
    multiple alternatives; for details, see https://goo.gl/tjCPAU.  Here we
    print only the transcription for the top alternative of the top result.

    In this case, responses are provided for interim results as well. If the
    response is an interim one, print a line feed at the end of it, to allow
    the next result to overwrite it, until the response is a final one. For the
    final one, print a newline to preserve the finalized transcription.

    Arg:
        responses: The responses returned from the API.
        stream: The audio stream to be processed.
    """
    last_cutoff = 0.0
    sentence_buffer = []
    
    for response in responses:
        if get_current_time() - stream.start_time > STREAMING_LIMIT:
            stream.start_time = get_current_time()
            break

        if not response.results:
            continue

        result = response.results[0]

        if not result.alternatives:
            continue

        transcript = result.alternatives[0].transcript

        result_seconds = 0
        result_micros = 0

        if result.result_end_time.seconds:
            result_seconds = result.result_end_time.seconds

        if result.result_end_time.microseconds:
            result_micros = result.result_end_time.microseconds

        stream.result_end_time = int((result_seconds * 1000) + (result_micros / 1000))

        corrected_time = (
            stream.result_end_time
            - stream.bridging_offset
            + (STREAMING_LIMIT * stream.restart_counter)
        )
        
        sentence_buffer.append(transcript)
        # Display interim results, but with a carriage return at the end of the
        # line, so subsequent lines will overwrite them.

        if len(sentence_buffer) > TRANSLATION_BUFFER or result.is_final:
            try:
                if any(isinstance(s, bytes) for s in sentence_buffer):
                    sentence_buffer = [s.decode('utf-8') if isinstance(s, bytes) else s for s in sentence_buffer]

                if not sentence_buffer or sentence_buffer[0] == "":
                    continue

                translation_response = translator.translate_text(request={
                    "contents": sentence_buffer,
                    "target_language_code": translation_target_language_code,
                    "source_language_code": translation_source_language_code,
                    "parent": f"projects/{project_id}/locations/{location}"
                })

                translated_sentences = [t.translated_text for t in translation_response.translations]
                translated_transcript = translated_sentences[-1]
                # translated_transcript = transcript
                
                if result.is_final:
                    sys.stdout.write(GREEN)
                    sys.stdout.write("\033[K")
                    
                    # output = str(corrected_time) + ": " + translated_transcript + "\n"
                    output = translated_transcript + "\n"
                    sys.stdout.write(output)
                    
                    js = {
                        "output": output,
                        "isFinal": result.is_final,
                    }
                    
                    future = publisher_client.publish(topic_name, json.dumps(js, ensure_ascii=False).encode("utf-8"), ordering_key="STT")
                    future.result()

                    stream.is_final_end_time = stream.result_end_time
                    stream.last_transcript_was_final = True
                    

                    # Exit recognition if any of the transcribed phrases could be
                    # one of our keywords.
                    # if re.search(r"\b(exit|quit)\b", transcript, re.I):
                    #     sys.stdout.write(YELLOW)
                    #     sys.stdout.write("Exiting...\n")
                    #     stream.closed = True
                    #     break
                else:
                    sys.stdout.write(RED)
                    sys.stdout.write("\033[K")
                    # output = str(corrected_time) + ": " + translated_transcript + "\r"
                    output = translated_transcript + "\r"
                    sys.stdout.write(output)
                    
                    js = {
                        "output": output,
                        "isFinal": result.is_final,
                    }
                    
                    future = publisher_client.publish(topic_name, json.dumps(js, ensure_ascii=False).encode("utf-8"), ordering_key="STT")
                    future.result()

                    stream.last_transcript_was_final = False
                    
                sentence_buffer = []
            except Exception as e:
                print(f"Translation error: {e}")
                sentence_buffer = []
                continue


def main() -> None:
    """start bidirectional streaming from microphone input to speech API"""
    client = speech.SpeechClient()
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=SAMPLE_RATE,
        language_code=stt_language_code,
        max_alternatives=1,
    )

    streaming_config = speech.StreamingRecognitionConfig(
        config=config, interim_results=True
    )

    mic_manager = ResumableMicrophoneStream(SAMPLE_RATE, CHUNK_SIZE)

    sys.stdout.write(YELLOW)
    sys.stdout.write('\nListening, say "Quit" or "Exit" to stop.\n\n')
    sys.stdout.write("End (ms)       Transcript Results/Status\n")
    sys.stdout.write("=====================================================\n")

    with mic_manager as stream:
        while not stream.closed:
            sys.stdout.write(YELLOW)
            sys.stdout.write(
                "\n" + str(STREAMING_LIMIT * stream.restart_counter) + ": NEW REQUEST\n"
            )

            stream.audio_input = []
            audio_generator = stream.generator()

            requests = (
                speech.StreamingRecognizeRequest(audio_content=content)
                for content in audio_generator
            )

            responses = client.streaming_recognize(streaming_config, requests)

            # Now, put the transcription responses to use.
            try:
                listen_print_loop(responses, stream)
            except Exception as exception:
                print(f"Caught exception: {exception}")
                stream.closed = True

            stream.audio_input = []
            stream.new_stream = True


if __name__ == "__main__":
    main()