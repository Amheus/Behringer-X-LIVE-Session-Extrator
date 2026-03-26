import struct
import wave
from pathlib import Path


class SessionSplittingUtilities:
    CHUNK_SIZE: int = 1024

    def __init__(
            self,
            input_directory_path: str,
            output_directory_path: str,
            filename_pattern: str = "*.WAV",
            channels_to_extract: int = None,
            naming_pattern: str = "{stem}_track_{channel:02d}.wav"
    ):
        self.input_folder_path = Path(input_directory_path)
        self.output_folder_path = Path(output_directory_path)
        self.filename_pattern = filename_pattern
        self.channels_to_extract = channels_to_extract
        self.naming_pattern = naming_pattern

        # populated from the first file in the series
        self.number_of_channels = None
        self.sample_width = None
        self.framerate = None

        self.output_wavs = None

        self.output_folder_path.mkdir(parents=True, exist_ok=True)

    # ====================================================================================================
    # SERIES DISCOVERY
    # ====================================================================================================
    def find_series_files(self) -> list[Path]:
        files = sorted(self.input_folder_path.glob(self.filename_pattern))
        if not files:
            raise FileNotFoundError(
                f"No files matching '{self.filename_pattern}' found in {self.input_folder_path}"
            )
        return files

    # ====================================================================================================
    # FORMAT READING / VALIDATION
    # ====================================================================================================
    @staticmethod
    def read_wav_format(path: Path) -> dict:
        with wave.open(str(path), 'rb') as wav:
            return {
                "channels": wav.getnchannels(),
                "sample_width": wav.getsampwidth(),
                "framerate": wav.getframerate(),
                "frames": wav.getnframes(),
            }

    @staticmethod
    def check_format_continuity(reference: dict, candidate: dict, path: Path):
        mismatches = []
        for key in ("channels", "sample_width", "framerate"):
            if reference[key] != candidate[key]:
                mismatches.append(
                    f"  {key}: expected {reference[key]}, got {candidate[key]}"
                )
        if mismatches:
            print(f"WARNING: {path.name} format mismatch - skipping file:")
            for m in mismatches:
                print(m)
            return False
        return True

    # ====================================================================================================
    # OUTPUT FILE OPERATIONS
    # ====================================================================================================
    def open_output_files(self, reference_stem: str):
        self.output_wavs = {}
        for channel_number in self.channels_to_extract:
            output_filename = self.naming_pattern.format(
                stem=reference_stem,
                filename=reference_stem,  # backwards compatibility
                channel=channel_number
            )
            output_filepath = self.output_folder_path / output_filename
            output_wav = wave.open(str(output_filepath), 'wb')
            output_wav.setnchannels(1)
            output_wav.setsampwidth(self.sample_width)
            output_wav.setframerate(self.framerate)
            self.output_wavs[channel_number] = output_wav

    def close_output_files(self):
        if self.output_wavs:
            for output_wav in self.output_wavs.values():
                output_wav.close()

    # ====================================================================================================
    # CHUNK PROCESSING
    # ====================================================================================================
    def process_chunks(self, input_wav: wave.Wave_read, total_frames: int):
        for _ in range(0, total_frames, self.CHUNK_SIZE):
            frames_to_read = min(self.CHUNK_SIZE, total_frames - input_wav.tell())
            if frames_to_read == 0:
                break

            data = input_wav.readframes(frames_to_read)

            if self.sample_width == 1:
                samples = struct.unpack(f'{len(data)}B', data)
            elif self.sample_width == 2:
                samples = struct.unpack(f'{len(data) // 2}h', data)
            elif self.sample_width == 3:
                samples = []
                for i in range(0, len(data), 3):
                    sample_bytes = data[i:i + 3]
                    if len(sample_bytes) == 3:
                        extended = sample_bytes + (b'\x00' if sample_bytes[2] < 128 else b'\xff')
                        sample = struct.unpack('<i', extended)[0]
                        samples.append(sample >> 8)
            elif self.sample_width == 4:
                samples = struct.unpack(f'{len(data) // 4}i', data)
            else:
                raise ValueError(f"Unsupported sample width: {self.sample_width}")

            for channel_num in self.channels_to_extract:
                channel_idx = channel_num - 1
                channel_samples = samples[channel_idx::self.number_of_channels]

                if self.sample_width == 1:
                    channel_data = struct.pack(f'{len(channel_samples)}B', *channel_samples)
                elif self.sample_width == 2:
                    channel_data = struct.pack(f'{len(channel_samples)}h', *channel_samples)
                elif self.sample_width == 3:
                    channel_data = b''
                    for sample in channel_samples:
                        sample_32 = sample << 8
                        sample_bytes = struct.pack('<i', sample_32)
                        channel_data += sample_bytes[:3]
                elif self.sample_width == 4:
                    channel_data = struct.pack(f'{len(channel_samples)}i', *channel_samples)

                self.output_wavs[channel_num].writeframes(channel_data)

    # ====================================================================================================
    # GO!
    # ====================================================================================================
    def go(self) -> bool:
        series = self.find_series_files()

        print(f"Found {len(series)} file(s) in series:")
        for f in series:
            print(f"  {f.name}")

        # read format from the first file; all subsequent files must match
        reference_fmt = self.read_wav_format(series[0])
        self.number_of_channels = reference_fmt["channels"]
        self.sample_width = reference_fmt["sample_width"]
        self.framerate = reference_fmt["framerate"]

        print(f"\nSession format (from {series[0].name}):")
        print(f"  Channels:     {self.number_of_channels}")
        print(f"  Sample rate:  {self.framerate} Hz")
        print(f"  Sample width: {self.sample_width} bytes")

        # resolve channels to extract
        if self.channels_to_extract is None:
            self.channels_to_extract = list(range(1, self.number_of_channels + 1))
        else:
            invalid = [ch for ch in self.channels_to_extract
                       if ch < 1 or ch > self.number_of_channels]
            if invalid:
                print(f"Warning: skipping invalid channel numbers: {invalid}")
            self.channels_to_extract = [ch for ch in self.channels_to_extract
                                        if 1 <= ch <= self.number_of_channels]

        if not self.channels_to_extract:
            print("No valid channels to extract.")
            return False

        print(f"  Extracting channels: {self.channels_to_extract}")

        # use the first file's stem as the base name for output tracks
        self.open_output_files(reference_stem=series[0].stem)

        total_frames_written = 0

        try:
            for path in series:
                fmt = self.read_wav_format(path)

                if path != series[0] and not self.check_format_continuity(reference_fmt, fmt, path):
                    continue  # skip mismatched files, keep going

                print(f"\nProcessing {path.name} "
                      f"({fmt['frames'] / self.framerate:.2f}s / "
                      f"{fmt['frames']} frames)")

                with wave.open(str(path), 'rb') as input_wav:
                    self.process_chunks(input_wav, fmt["frames"])

                total_frames_written += fmt["frames"]

        finally:
            self.close_output_files()

        total_duration = total_frames_written / self.framerate
        print(f"\nExtraction complete.")
        print(f"  Total duration:  {total_duration:.2f}s")
        print(f"  Output tracks:   {len(self.channels_to_extract)}")
        return True
