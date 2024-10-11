
import re
import sys
import time

from google.cloud import translate
from google.cloud import pubsub_v1


from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech as cloud_speech_types

import pyaudio
import queue
import json

STREAMING_LIMIT = 240000  # 4 minutes in millisecones - Google Cloud STT limit max at 5 minutes
SAMPLE_RATE = 16000
CHUNK_SIZE = int(SAMPLE_RATE / 10)

translator = translate.TranslationServiceClient()
client = SpeechClient()

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"

project_id = "{REPLACE_WITH_PROJECT_ID}"
location = "global"

stt_language_code = "th-TH"

translation_source_language_code = "th"
translation_target_language_code = "en-US"


publisher_client = pubsub_v1.PublisherClient()
topic_name = "projects/{project_id}/topics/{topic}".format(
    project_id=project_id,
    topic="live-translation"
)


def get_current_time():
    return int(round(time.time() * 1000))


class ResumableMicrophoneStream:
    def __init__(self, rate, chunk_size):
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
            stream_callback=self._fill_buffer,
        )

    def __enter__(self):

        self.closed = False
        return self

    def __exit__(self, type, value, traceback):

        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        self._buff.put(None)
        self._audio_interface.terminate()

    def _fill_buffer(self, in_data, *args, **kwargs):
        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def generator(self):
        """Stream Audio from microphone to API and to local buffer"""

        while not self.closed:
            data = []

            if self.new_stream and self.last_audio_input:

                chunk_time = STREAMING_LIMIT / len(self.last_audio_input)

                if chunk_time != 0:

                    if self.bridging_offset < 0:
                        self.bridging_offset = 0

                    if self.bridging_offset > self.final_request_end_time:
                        self.bridging_offset = self.final_request_end_time

                    chunks_from_ms = round(
                        (self.final_request_end_time - self.bridging_offset)
                        / chunk_time
                    )

                    self.bridging_offset = round(
                        (len(self.last_audio_input) - chunks_from_ms) * chunk_time
                    )

                    for i in range(chunks_from_ms, len(self.last_audio_input)):
                        data.append(self.last_audio_input[i])

                self.new_stream = False

            chunk = self._buff.get()
            self.audio_input.append(chunk)

            if chunk is None:
                return
            data.append(chunk)

            while True:
                try:
                    chunk = self._buff.get(block=False)

                    if chunk is None:
                        return
                    data.append(chunk)
                    self.audio_input.append(chunk)

                except queue.Empty:
                    break

            yield b"".join(data)


def listen_print_loop(responses, stream):
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
    """
    num_chars_printed = 0
    for response in responses:
        # print(response)
        if get_current_time() - stream.start_time > STREAMING_LIMIT:
            stream.start_time = get_current_time()
            break

        if not response.results:
            continue

        result = response.results[0]

        if not result.alternatives:
            continue

        transcript = result.alternatives[0].transcript

        if isinstance(transcript, bytes):
            transcript = transcript.decode("utf-8")

        translation_response = translator.translate_text(request={
            "contents": [transcript],
            "target_language_code": translation_target_language_code,
            "source_language_code": translation_source_language_code,
            "parent": f"projects/{project_id}/locations/{location}"
        })

        transcript = ""
        for translation in translation_response.translations:
            transcript += translation.translated_text

        result_seconds = 0
        result_micros = 0

        if result.result_end_offset.seconds:
            result_seconds = result.result_end_offset.seconds

        if result.result_end_offset.microseconds:
            result_micros = result.result_end_offset.microseconds

        stream.result_end_time = int((result_seconds * 1000) + (result_micros / 1000))

        # corrected_time = (
        #     stream.result_end_time
        #     - stream.bridging_offset
        #     + (STREAMING_LIMIT * stream.restart_counter)
        # )
        
        # Display interim results, but with a carriage return at the end of the
        # line, so subsequent lines will overwrite them.

        # If the previous result was longer than this one, we need to print
        # some extra spaces to overwrite the previous result
        overwrite_chars = " " * (num_chars_printed - len(transcript))

        if result.is_final:
            sys.stdout.write(GREEN)
            sys.stdout.write("\033[K")

            # output = str(corrected_time) + ": " + transcript + "\n"
            output = transcript + "\n"
            sys.stdout.write(output)

            js = {
                "output": output,
                "is_final": result.is_final,
            }

            future = publisher_client.publish(topic_name, json.dumps(js, ensure_ascii=False).encode("utf-8"))
            future.result()

            stream.is_final_end_time = stream.result_end_time
            stream.last_transcript_was_final = True

        else:
            sys.stdout.write(RED)
            sys.stdout.write("\033[K")

            output = transcript + overwrite_chars + "\r"
            sys.stdout.write(output)
            sys.stdout.flush()

            js = {
                "output": output,
                "is_final": result.is_final,
            }

            future = publisher_client.publish(topic_name, json.dumps(js, ensure_ascii=False).encode("utf-8"))
            future.result()

            num_chars_printed = len(transcript)
            stream.last_transcript_was_final = False


def start_live_translation():
    """start bidirectional streaming from microphone input to speech API"""

    config = cloud_speech_types.RecognitionConfig(
        auto_decoding_config=cloud_speech_types.AutoDetectDecodingConfig(),
        explicit_decoding_config=cloud_speech_types.ExplicitDecodingConfig(
            encoding=cloud_speech_types.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=SAMPLE_RATE,
            audio_channel_count=1
        ),
        language_codes=[stt_language_code],
        model="long"
    )

    streaming_config = cloud_speech_types.StreamingRecognitionConfig(
        config=config,
        streaming_features=cloud_speech_types.StreamingRecognitionFeatures(
            interim_results=True # enable interim results, otherwise only final results will be returned
        )
    )

    config_request = cloud_speech_types.StreamingRecognizeRequest(
        recognizer=f"projects/{project_id}/locations/global/recognizers/_",
        streaming_config=streaming_config,
    )

    mic_manager = ResumableMicrophoneStream(SAMPLE_RATE, CHUNK_SIZE)
    print(mic_manager.chunk_size)
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
            audio_generator = stream.generator()  # receive audio content

            # passing audio contents to request
            audio_requests = (
                cloud_speech_types.StreamingRecognizeRequest(audio=content)
                for content in audio_generator
            )

            def requests(config: cloud_speech_types.RecognitionConfig, audio: list):
                yield config
                yield from audio

            # calling API request and receive response
            responses_iterator = client.streaming_recognize(
                requests=requests(config_request, audio_requests)
            )

            listen_print_loop(responses_iterator, stream)

            # reset time and audio content when sending new request
            if stream.result_end_time > 0:
                stream.final_request_end_time = stream.is_final_end_time
            stream.result_end_time = 0
            stream.last_audio_input = []
            stream.last_audio_input = stream.audio_input
            stream.audio_input = []
            stream.restart_counter = stream.restart_counter + 1

            if not stream.last_transcript_was_final:
                sys.stdout.write("\n")
            # start new stream
            stream.new_stream = True


if __name__ == "__main__":
    start_live_translation()
