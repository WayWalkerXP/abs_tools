abs_move.py

Requirements:
pyyaml
mutagen

for options, run
python abs_move.py --help

Default location for config.yml is ~/.config/abs/config.yml

Example run:
python abs_move.py /media/audiobooks/working/shelf --replace_spaces="_" --duplicates=ask

For dry-run, add --dryrun. When a dry run is performed, no changes are made, but all proposed changes are stored in a CSV file. I highly recommend using this option the first time it is run.
