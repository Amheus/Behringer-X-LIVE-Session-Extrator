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


def main() -> None:
    print("=" * 50)
    print("Behringer X-LIVE Session Extraction Tool")
    print("=" * 50)
    print()

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
