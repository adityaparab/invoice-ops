"""One-shot seed entry point; synthetic ERP generation is plan step 2.1."""

import logging


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger(__name__).info(
        '{"event":"seed.placeholder","status":"no_data_written","plan_step":"2.1"}'
    )


if __name__ == "__main__":
    main()
