# Extract a spectrogram for every image in the specified folder.
# This is used after searching, which generates images.
# Delete the images you don't want to keep, then run this to import the rest as training data.
# Note that image file names must be "filename-offset.png", e.g. "XC1000-4.5.png" corresponds to
# offset 4.5 of recording XC1000.mp3.

import argparse
import inspect
import os
import sys
import time
from pathlib import Path

# this is necessary before importing from a peer directory
currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parentdir = os.path.dirname(currentdir)
sys.path.insert(0, parentdir)

from core import extractor
from core import util

class ExtractByImage(extractor.Extractor):
    def __init__(self, audio_path, images_path, db_name, source, category, species_name, species_code, low_band):
        super().__init__(audio_path, db_name, source, category, species_name, species_code, low_band)
        self.images_path = images_path

    # get list of specs from directory of images
    def _process_image_dir(self):
        self.offsets = {}
        for image_path in Path().glob(f"{self.images_path}/*.png"):
            name = Path(image_path).stem
            tokens = name.split('-')
            offset = tokens[-1]
            file_name = name[:-(len(offset)+1)]

            if file_name not in self.offsets:
                self.offsets[file_name] = []

            self.offsets[file_name].append(float(offset))

    def run(self):
        self._process_image_dir()
        num_inserted = 0
        for recording_path in self.get_recording_paths():
            filename = Path(recording_path).stem
            if filename not in self.offsets:
                continue

            print(f"Processing {recording_path}")
            self.insert_spectrograms(recording_path, self.offsets[filename])
            num_inserted += len(self.offsets[filename])

        print(f"Inserted {num_inserted} spectrograms.")

if __name__ == '__main__':

    # command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('-a', type=str, default=None, help='Source of recordings. By default, use the file names to get the source.')
    parser.add_argument('-b', type=str, default='bird', help='Category. Default = "bird"')
    parser.add_argument('-c', type=str, default=None, help='Species code (required)')
    parser.add_argument('-d', type=str, default=None, help='Directory containing recordings (required).')
    parser.add_argument('-f', type=str, default='training', help='Database name or full path ending in ".db". Default = "training"')
    parser.add_argument('-i', type=str, default=None, help='Directory containing spectrogram images (required).')
    parser.add_argument('-l', type=int, default=0, help='1 = low band (default=0)')
    parser.add_argument('-s', type=str, default=None, help='Species name (required)')

    args = parser.parse_args()
    if args.d is None:
        print("Error: -d argument is required (directory containing recordings).")
        quit()
    else:
        audio_path = args.d

    if args.i is None:
        print("Error: -i argument is required (directory containing images).")
        quit()
    else:
        image_path = args.i

    if args.s is None:
        print("Error: -s argument is required (species name).")
        quit()
    else:
        species_name = args.s

    if args.c is None:
        print("Error: -c argument is required (species code).")
        quit()
    else:
        species_code = args.c

    run_start_time = time.time()

    ExtractByImage(audio_path, image_path, args.f, args.a, args.b, species_name, species_code, args.l).run()

    elapsed = time.time() - run_start_time
    print(f'elapsed seconds = {elapsed:.1f}')
