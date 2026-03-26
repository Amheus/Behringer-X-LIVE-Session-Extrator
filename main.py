import argparse

from common.session_splitting_utilities import SessionSplittingUtilities


def run_wizard() -> dict:
    print("Config Wizard:")
    print("1: Input folder path                      > ", end="")
    input_directory_path: str = input()

    print("2: Output folder path                     > ", end="")
    output_directory_path: str = input()

    print("3: Channels to extract    [blank for all] > ", end="")
    channels_input: str = input().strip()
    channels_to_extract = None
    if channels_input:
        try:
            channels_to_extract = [int(ch.strip()) for ch in channels_input.replace(',', ' ').split()]
        except ValueError:
            print("Warning: Invalid channel format. Extracting all channels.")
            channels_to_extract = None

    print("4: Naming pattern         [{filename}_track_{channel:02d}.wav] > ", end="")
    naming_pattern: str = input().strip()
    if not naming_pattern:
        naming_pattern = "{filename}_track_{channel:02d}.wav"

    return {
        'input_directory_path': input_directory_path,
        'output_directory_path': output_directory_path,
        'channels_to_extract': channels_to_extract,
        'naming_pattern': naming_pattern
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Behringer X-LIVE Session Extraction Tool"
    )
    parser.add_argument(
        "-i", "--input",
        dest="input_directory_path",
        help="Input folder path"
    )
    parser.add_argument(
        "-o", "--output",
        dest="output_directory_path",
        help="Output folder path"
    )
    parser.add_argument(
        "-c", "--channels",
        dest="channels_to_extract",
        nargs="+",
        type=int,
        metavar="CHANNEL",
        help="Channels to extract (e.g. --channels 1 2 3). Omit for all channels."
    )
    parser.add_argument(
        "-n", "--naming-pattern",
        dest="naming_pattern",
        default="{filename}_track_{channel:02d}.wav",
        help='Output naming pattern (default: "{filename}_track_{channel:02d}.wav")'
    )
    return parser.parse_args()


def main() -> None:
    print("=" * 50)
    print("Behringer X-LIVE Session Extraction Tool")
    print("=" * 50)
    print()

    args = parse_args()

    # Use CLI args if both required paths were provided, otherwise run the wizard
    if args.input_directory_path and args.output_directory_path:
        config = {
            'input_directory_path': args.input_directory_path,
            'output_directory_path': args.output_directory_path,
            'channels_to_extract': args.channels_to_extract,
            'naming_pattern': args.naming_pattern
        }
    else:
        config = run_wizard()

    print()
    print("=" * 50)
    print()

    ssu = SessionSplittingUtilities(
        input_directory_path=config['input_directory_path'],
        output_directory_path=config['output_directory_path'],
        channels_to_extract=config['channels_to_extract'],
        naming_pattern=config['naming_pattern']
    )
    ssu.go()


if __name__ == '__main__':
    main()
