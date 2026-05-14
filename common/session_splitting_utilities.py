import struct
import wave
from pathlib import Path

DEFAULT_NUMERIC_NAMING_PATTERN = "{stem}_track_{channel:02d}.wav"
DEFAULT_NAMED_NAMING_PATTERN = "ch{channel:02d}.{name}.wav"

_ILLEGAL_FILENAME_CHARS = '<>:"/\\|?*'


def _sanitise_filename_component(s: str) -> str:
    return ''.join('_' if c in _ILLEGAL_FILENAME_CHARS else c for c in s).strip()


class SessionSplittingUtilities:
    CHUNK_SIZE: int = 1024

    def __init__(
            self,
            input_directory_path: str,
            output_directory_path: str,
            filename_pattern: str = "*.WAV",
            channels_to_extract: list[int] | None = None,
            channel_names: list[str | None] | None = None,
            naming_pattern: str | None = None,
            mix_filename_pattern: str | None = "mix.wav",
    ):
        self.input_folder_path = Path(input_directory_path)
        self.output_folder_path = Path(output_directory_path)
        self.filename_pattern = filename_pattern
        self.channel_names = channel_names
        self.mix_filename_pattern = mix_filename_pattern

        if channel_names is not None:
            # derive extraction list from the map; the map is the source of truth
            self.channels_to_extract = [
                i + 1 for i, name in enumerate(channel_names) if name is not None
            ]
            self.naming_pattern = naming_pattern or DEFAULT_NAMED_NAMING_PATTERN
        else:
            self.channels_to_extract = channels_to_extract
            self.naming_pattern = naming_pattern or DEFAULT_NUMERIC_NAMING_PATTERN

        # populated from the first file in the series
        self.number_of_channels = None
        self.sample_width = None
        self.framerate = None

        self.output_wavs = None
        self.mix_wav = None
        # files are written here while processing,
        # then atomically renamed into output_folder_path on success
        # - keeps the destination free of half-written files until extraction is complete
        self.staging_folder_path = self.input_folder_path / ".partial"
        self.staged_paths: list[Path] = []

        self.output_folder_path.mkdir(parents=True, exist_ok=True)

    # ==================================================
    # SERIES DISCOVERY
    # ==================================================
    def find_series_files(self) -> list[Path]:
        files = sorted(self.input_folder_path.glob(self.filename_pattern))
        if not files:
            raise FileNotFoundError(f"No files matching '{self.filename_pattern}' found in {self.input_folder_path}")
        return files

    # ==================================================
    # FORMAT READING / VALIDATION
    # ==================================================
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
                mismatches.append(f"  {key}: expected {reference[key]}, got {candidate[key]}")
        if mismatches:
            print(f"WARNING: {path.name} format mismatch - skipping file:")
            for m in mismatches:
                print(m)
            return False
        return True

    # ==================================================
    # OUTPUT FILE OPERATIONS
    # ==================================================
    def open_output_files(self, reference_stem: str):
        # clear any leftovers from a previous aborted run before we start
        if self.staging_folder_path.exists():
            for p in self.staging_folder_path.iterdir():
                try:
                    p.unlink()
                except OSError:
                    pass
        self.staging_folder_path.mkdir(parents=True, exist_ok=True)

        self.output_wavs = {}
        self.staged_paths = []
        for channel_number in self.channels_to_extract:
            format_kwargs = {
                "stem": reference_stem,
                "filename": reference_stem,  # backwards compatibility
                "channel": channel_number,
            }
            if self.channel_names is not None:
                raw_name = self.channel_names[channel_number - 1]
                format_kwargs["name"] = _sanitise_filename_component(raw_name)

            output_filename = self.naming_pattern.format(**format_kwargs)
            staged_filepath = self.staging_folder_path / output_filename
            self.staged_paths.append(staged_filepath)
            output_wav = wave.open(str(staged_filepath), 'wb')
            output_wav.setnchannels(1)
            output_wav.setsampwidth(self.sample_width)
            output_wav.setframerate(self.framerate)
            self.output_wavs[channel_number] = output_wav

        # mix file (mono average of all extracted channels), if enabled
        if self.mix_filename_pattern:
            mix_filename = self.mix_filename_pattern.format(stem=reference_stem)
            mix_staged_path = self.staging_folder_path / mix_filename
            self.staged_paths.append(mix_staged_path)
            self.mix_wav = wave.open(str(mix_staged_path), 'wb')
            self.mix_wav.setnchannels(1)
            self.mix_wav.setsampwidth(self.sample_width)
            self.mix_wav.setframerate(self.framerate)

    def close_output_files(self):
        if self.output_wavs:
            for output_wav in self.output_wavs.values():
                try:
                    output_wav.close()
                except Exception:
                    pass
            self.output_wavs = None
        if self.mix_wav is not None:
            try:
                self.mix_wav.close()
            except Exception:
                pass
            self.mix_wav = None

    def promote_staged_outputs(self):
        for staged in self.staged_paths:
            final = self.output_folder_path / staged.name
            staged.replace(final)  # atomic on the same filesystem
        try:
            self.staging_folder_path.rmdir()
        except OSError:
            pass

    def cleanup_staged_outputs(self):
        for p in self.staged_paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        # belt and braces: also clear any other strays so the dir can rmdir
        if self.staging_folder_path.exists():
            for p in self.staging_folder_path.iterdir():
                try:
                    p.unlink()
                except OSError:
                    pass
            try:
                self.staging_folder_path.rmdir()
            except OSError:
                pass

    # ==================================================
    # CHUNK PROCESSING
    # ==================================================
    def _unpack_samples(self, data: bytes) -> list[int]:
        if self.sample_width == 1:
            return list(struct.unpack(f'{len(data)}B', data))
        elif self.sample_width == 2:
            return list(struct.unpack(f'{len(data) // 2}h', data))
        elif self.sample_width == 3:
            # sign-extend each 24-bit sample into a 32-bit container, then unpack as int32 - gives the correct signed 24-bit value
            samples = []
            for i in range(0, len(data), 3):
                sb = data[i:i + 3]
                if len(sb) == 3:
                    extended = sb + (b'\x00' if sb[2] < 128 else b'\xff')
                    samples.append(struct.unpack('<i', extended)[0])
            return samples
        elif self.sample_width == 4:
            return list(struct.unpack(f'{len(data) // 4}i', data))
        else:
            raise ValueError(f"Unsupported sample width: {self.sample_width}")

    def _pack_samples(self, samples) -> bytes:
        if self.sample_width == 1:
            return struct.pack(f'{len(samples)}B', *samples)
        elif self.sample_width == 2:
            return struct.pack(f'{len(samples)}h', *samples)
        elif self.sample_width == 3:
            # pack as int32, drop the high byte (it's sign extension only - the value already fits in 24 bits)
            return b''.join(struct.pack('<i', s)[:3] for s in samples)
        elif self.sample_width == 4:
            return struct.pack(f'{len(samples)}i', *samples)
        else:
            raise ValueError(f"Unsupported sample width: {self.sample_width}")

    def process_chunks(self, input_wav: wave.Wave_read, total_frames: int):
        n_extracted = len(self.channels_to_extract)

        for _ in range(0, total_frames, self.CHUNK_SIZE):
            frames_to_read = min(self.CHUNK_SIZE, total_frames - input_wav.tell())
            if frames_to_read == 0:
                break

            data = input_wav.readframes(frames_to_read)
            samples = self._unpack_samples(data)

            # split interleaved samples into one slice per extracted channel
            channel_slices = {
                ch: samples[(ch - 1)::self.number_of_channels]
                for ch in self.channels_to_extract
            }

            # write each channel's mono file
            for channel_num, channel_samples in channel_slices.items():
                self.output_wavs[channel_num].writeframes(
                    self._pack_samples(channel_samples)
                )

            # write the mono mix: average of all extracted channels at each time index.
            # Averaging (rather than summing) guarantees the mix cannot clip
            # - at the cost of being quiet in proportion to the number of channels in the map.
            # Boost in a DAW if needed.
            if self.mix_wav is not None:
                mix_samples = [
                    sum(group) // n_extracted
                    for group in zip(*channel_slices.values())
                ]
                self.mix_wav.writeframes(self._pack_samples(mix_samples))

    # ==================================================
    # GO!
    # ==================================================
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

        # if a channel map was supplied, sanity-check it against the actual channel count before resolving the extraction list
        if self.channel_names is not None:
            if len(self.channel_names) != self.number_of_channels:
                print(
                    f"Warning: channel map has {len(self.channel_names)} entries "
                    f"but the WAV has {self.number_of_channels} channels - "
                    f"any out-of-range entries will be skipped."
                )
            # rebuild from the map, clipped to what the file actually has
            self.channels_to_extract = [
                i + 1 for i, name in enumerate(self.channel_names)
                if name is not None and 1 <= i + 1 <= self.number_of_channels
            ]

        # resolve channels to extract
        if self.channels_to_extract is None:
            self.channels_to_extract = list(range(1, self.number_of_channels + 1))
        else:
            invalid = [ch for ch in self.channels_to_extract if ch < 1 or ch > self.number_of_channels]
            if invalid:
                print(f"Warning: skipping invalid channel numbers: {invalid}")
            self.channels_to_extract = [ch for ch in self.channels_to_extract if 1 <= ch <= self.number_of_channels]

        if not self.channels_to_extract:
            print("No valid channels to extract.")
            return False

        if self.channel_names is not None:
            print("  Extracting channels:")
            for ch in self.channels_to_extract:
                print(f"    {ch:>2}: {self.channel_names[ch - 1]}")
        else:
            print(f"  Extracting channels: {self.channels_to_extract}")

        if self.mix_filename_pattern:
            print(f"  Plus mono mix (averaged across {len(self.channels_to_extract)} channels)")

        # use the first file's stem as the base name for output tracks
        self.open_output_files(reference_stem=series[0].stem)

        total_frames_written = 0
        success = False

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

            success = True
        finally:
            self.close_output_files()
            if success:
                # outputs are sitting in staging - move them into place atomically
                self.promote_staged_outputs()
            else:
                # bin the half-written outputs rather than leaving them around
                # to be confused with valid tracks
                self.cleanup_staged_outputs()

        total_duration = total_frames_written / self.framerate
        print(f"\nExtraction complete.")
        print(f"  Total duration:  {total_duration:.2f}s")
        print(f"  Output tracks:   {len(self.channels_to_extract)}")
        return True
