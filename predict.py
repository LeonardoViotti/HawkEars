"""
Adapted from analyze.py. Main changes:
 - Look for audio in subdirectories
 - Export a CSV file
 - Filter to just selected species.

"""


# Analyze an audio file, or all audio files in a directory.
# For each audio file, extract spectrograms, analyze them and output an Audacity label file
# with the class predictions.

import argparse
import glob
import logging
import multiprocessing as mp
import os
from pathlib import Path
import pickle
import re
import threading
import time
import zlib

import numpy as np
import pandas as pd
import torch

import species_handlers
from core import audio
from core import cfg
from core import filters
from core import frequency_db
from core import util
from model import main_model

class ClassInfo:
    def __init__(self, name, code, ignore):
        self.name = name
        self.code = code
        self.ignore = ignore
        self.max_frequency = 0
        self.is_bird = True
        self.reset()

    def reset(self):
        self.ebird_frequency_too_low = False
        self.has_label = False
        self.scores = []     # predictions (one per segment)
        self.is_label = []   # True iff corresponding offset is a label

class Label:
    def __init__(self, class_name, score, start_time, end_time):
        self.class_name = class_name
        self.score = score
        self.start_time = start_time
        self.end_time = end_time

class Analyzer:
    def __init__(self, input_path, output_path, start_time, end_time, date_str, latitude, longitude, region,
                 filelist, debug_mode, merge, overlap, device, thread_num=1, embed=False):
        self.input_path = input_path.strip()
        self.output_path = output_path.strip()
        self.start_seconds = self._get_seconds_from_time_string(start_time)
        self.end_seconds = self._get_seconds_from_time_string(end_time)
        self.date_str = date_str
        self.latitude = latitude
        self.longitude = longitude
        self.region = region
        self.filelist = filelist
        self.debug_mode = debug_mode
        self.overlap = overlap
        self.thread_num = thread_num
        self.embed = embed
        self.device = device
        self.frequencies = {}
        self.issued_skip_files_warning = False
        self.have_rarities_directory = False

        if cfg.infer.do_lpf:
            self.low_pass_filter = filters.low_pass_filter(cfg.infer.lpf_start_freq, cfg.infer.lpf_end_freq, cfg.infer.lpf_damp)

        if cfg.infer.do_hpf:
            self.high_pass_filter = filters.high_pass_filter(cfg.infer.hpf_start_freq, cfg.infer.hpf_end_freq, cfg.infer.hpf_damp)

        if cfg.infer.do_bpf:
            self.band_pass_filter = filters.band_pass_filter(cfg.infer.bpf_start_freq, cfg.infer.bpf_end_freq, cfg.infer.bpf_damp)

        if cfg.infer.min_score == 0:
            self.merge_labels = False # merging all labels >= min_score makes no sense in this case
        else:
            self.merge_labels = (merge == 1)

        if self.start_seconds is not None and self.end_seconds is not None and self.end_seconds < self.start_seconds + cfg.audio.segment_len:
                logging.error(f"Error: end time must be >= start time + {cfg.audio.segment_len} seconds")
                quit()

        if self.end_seconds is not None:
            self.end_seconds -= cfg.audio.segment_len # convert from end of last segment to start of last segment for processing

        # if no output path is specified, put the output labels in the input directory
        if len(self.output_path) == 0:
            if os.path.isdir(self.input_path):
                self.output_path = self.input_path
            else:
                self.output_path = Path(self.input_path).parent
        elif not os.path.exists(self.output_path):
            os.makedirs(self.output_path)

        # save labels here if they were excluded because of location/date processing
        self.rarities_output_path = os.path.join(self.output_path, 'rarities')

    @staticmethod
    def _get_file_list(input_path):
        if os.path.isdir(input_path):
            return util.get_audio_files(input_path)
        elif util.is_audio_file(input_path):
            return [input_path]
        else:
            logging.error(f"Error: {input_path} is not a directory or an audio file")
            quit()

    # return week number in the range [1, 48] as used by eBird barcharts, i.e. 4 weeks per month
    @staticmethod
    def _get_week_num_from_date_str(date_str):
        if not isinstance(date_str, str):
            return None # e.g. if filelist is used to filter recordings and no date is specified

        date_str = date_str.replace('-', '') # for case with yyyy-mm-dd dates in CSV file
        if not date_str.isnumeric():
            return None

        if len(date_str) >= 4:
            month = int(date_str[-4:-2])
            day = int(date_str[-2:])
            week_num = (month - 1) * 4 + min(4, (day - 1) // 7 + 1)
            return week_num
        else:
            return None

    # process latitude, longitude, region and date arguments;
    # a region is an alternative to lat/lon, and may specify an eBird county (e.g. CA-AB-FN)
    # or province (e.g. CA-AB)
    def _process_location_and_date(self):
        if self.filelist is None and self.region is None and (self.latitude is None or self.longitude is None):
            self.check_frequency = False
            self.week_num = None
            return

        self.check_frequency = True
        self.get_date_from_file_name = False
        self.freq_db = frequency_db.Frequency_DB()
        self.counties = self.freq_db.get_all_counties()
        self.ebird_species_names = {}
        results = self.freq_db.get_all_species()
        for r in results:
            self.ebird_species_names[r.name] = 1

        # if a location file is specified, use that
        self.week_num = None
        self.location_date_dict = None
        if self.filelist is not None:
            if os.path.exists(self.filelist):
                dataframe = pd.read_csv(self.filelist)
                expected_column_names = ['filename', 'latitude', 'longitude', 'recording_date']
                if len(dataframe.columns) != len(expected_column_names):
                    logging.error(f"Error: file {self.filelist} has {len(dataframe.columns)} columns but {len(expected_column_names)} were expected.")
                    quit()

                for i, column_name in enumerate(dataframe.columns):
                    if column_name != expected_column_names[i]:
                        logging.error(f"Error: file {self.filelist}, column {i} is {column_name} but {expected_column_names[i]} was expected.")
                        quit()

                self.location_date_dict = {}
                for i, row in dataframe.iterrows():
                    week_num = self._get_week_num_from_date_str(row['recording_date'])
                    self.location_date_dict[row['filename']] = [row['latitude'], row['longitude'], week_num]

                return
            else:
                logging.error(f"Error: file {self.filelist} not found.")
                quit()

        if self.date_str == 'file':
            self.get_date_from_file_name = True
        elif self.date_str is not None:
            self.week_num = self._get_week_num_from_date_str(self.date_str)
            if self.week_num is None:
                logging.error(f'Error: invalid date string: {self.date_str}')
                quit()

        counties = [] # list of relevant eBird counties
        if self.region is not None:
            for c in self.counties:
                if c.code.startswith(self.region):
                    counties.append(c)
        else:
            # use latitude/longitude and just pick one eBird county
            for c in self.counties:
                if self.latitude >= c.min_y and self.latitude <= c.max_y and self.longitude >= c.min_x and self.longitude <= c.max_x:
                    counties.append(c)
                    break

        if len(counties) == 0:
            if self.region is None:
                logging.error(f'Error: no eBird county found matching given latitude and longitude')
            else:
                logging.error(f'Error: no eBird county found matching given region')
            quit()
        elif len(counties) == 1:
            logging.info(f'Matching species in {counties[0].name} ({counties[0].code})')
        else:
            logging.info(f'Matching species in region {self.region}')

        self._update_class_frequency_stats(counties)

    # cache eBird species frequencies for performance
    def _get_frequencies(self, county_id, class_name):
        if county_id not in self.frequencies:
            self.frequencies[county_id] = {}

        if class_name in cfg.infer.ebird_names:
            # switch to the name that eBird uses
            class_name = cfg.infer.ebird_names[class_name]

        if class_name in self.frequencies[county_id]:
            return self.frequencies[county_id][class_name]
        else:
            results = self.freq_db.get_frequencies(county_id, class_name)
            self.frequencies[county_id][class_name] = results
            return results

    # update the weekly frequency data per species, where frequency is the
    # percent of eBird checklists containing a species in a given county/week;
    def _update_class_frequency_stats(self, counties):
        class_infos = {}
        for class_info in self.class_infos:
            if not class_info.name in cfg.infer.ebird_names and not class_info.name in self.ebird_species_names:
                class_info.is_bird = False
                continue

            class_infos[class_info.name] = class_info # copy from list to dict for faster reference
            if not class_info.ignore:
                # get sums of weekly frequencies for this species across specified counties
                frequency = [0 for i in range(48)] # eBird uses 4 weeks per month
                for county in counties:
                    results = self._get_frequencies(county.id, class_info.name)
                    for i in range(len(results)):
                        # for each week use the maximum of it and the adjacent weeks
                        frequency[i] = max(max(results[i].value, results[(i + 1) % 48].value), results[(i - 1) % 48].value)

                if len(counties) > 1:
                    # get the average across counties
                    for week_num in range(48):
                        frequency[week_num] /= len(counties)

                # update the info associated with this species
                class_info.frequency = [0 for i in range(48)]
                class_info.max_frequency = 0
                for week_num in range(48):
                    # if no date is specified we will use the maximum across all weeks
                    class_info.max_frequency = max(class_info.max_frequency, frequency[week_num])
                    class_info.frequency[week_num] = frequency[week_num]

    # get class names and codes from the model, which gets them from the checkpoint
    def _get_class_infos(self):
        class_names = self.models[0].train_class_names
        class_codes = self.models[0].train_class_codes
        ignore_list = util.get_file_lines(cfg.misc.ignore_file)

        class_infos = []
        for i, class_name in enumerate(class_names):
            class_infos.append(ClassInfo(class_name, class_codes[i], class_name in ignore_list))

        return class_infos

    # return the average prediction of all models in the ensemble
    def _call_models(self, specs):
        # get predictions for each model
        predictions = []
        for model in self.models:
            model.to(self.device)
            predictions.append(model.get_predictions(specs, self.device, use_softmax=False))

        # calculate and return the average across models
        avg_pred = None
        for pred in predictions:
            if avg_pred is None:
                avg_pred = pred
            else:
                avg_pred += pred

        avg_pred /= len(predictions)
        return avg_pred ** cfg.infer.score_exponent

    # get predictions using a low-pass, high-pass or band-pass filter,
    # and then set each score to the max of the filtered and unfiltered score
    def _apply_filter(self, original_specs, filter):
        specs = original_specs.copy()
        for i, spec in enumerate(specs):
            spec = spec.reshape((cfg.audio.spec_height, cfg.audio.spec_width))
            specs[i] = (spec.T * filter).T

        predictions = self._call_models(specs)
        for i in range(len(specs)):
            for j in range(len(self.class_infos)):
                if self.class_infos[j].ignore:
                    continue

                self.class_infos[j].scores[i] = max(self.class_infos[j].scores[i], predictions[i][j])
                if (self.class_infos[j].scores[i] >= cfg.infer.min_score):
                    self.class_infos[j].has_label = True

    def _get_predictions(self, signal, rate):
        # if needed, pad the signal with zeros to get the last spectrogram
        total_seconds = signal.shape[0] / rate
        last_segment_len = total_seconds - cfg.audio.segment_len * (total_seconds // cfg.audio.segment_len)
        if last_segment_len > 0.5:
            # more than 1/2 a second at the end, so we'd better analyze it
            pad_amount = int(rate * (cfg.audio.segment_len - last_segment_len)) + 1
            signal = np.pad(signal, (0, pad_amount), 'constant', constant_values=(0, 0))

        start_seconds = 0 if self.start_seconds is None else self.start_seconds
        max_end_seconds = max(0, (signal.shape[0] / rate) - cfg.audio.segment_len)
        end_seconds = max_end_seconds if self.end_seconds is None else self.end_seconds

        specs = self._get_specs(start_seconds, end_seconds)
        logging.debug(f"Analyzing from {start_seconds} to {end_seconds} seconds")
        logging.debug(f"Retrieved {len(specs)} spectrograms")

        if cfg.infer.do_unfiltered:
            predictions = self._call_models(specs)

            if self.debug_mode:
                self._log_predictions(predictions)

        # populate class_infos with predictions using unfiltered spectrograms
        for i in range(len(self.offsets)):
            for j in range(len(self.class_infos)):
                if cfg.infer.do_unfiltered:
                    self.class_infos[j].scores.append(predictions[i][j])
                else:
                    self.class_infos[j].scores.append(0)

                self.class_infos[j].is_label.append(False)
                if (self.class_infos[j].scores[-1] >= cfg.infer.min_score):
                    self.class_infos[j].has_label = True

        # optionally process low-pass, high-pass and band-pass filters
        if cfg.infer.do_lpf:
            self._apply_filter(specs, self.low_pass_filter)

        if cfg.infer.do_hpf:
            self._apply_filter(specs, self.high_pass_filter)

        if cfg.infer.do_bpf:
            self._apply_filter(specs, self.band_pass_filter)

        # optionally generate embeddings
        if self.embed:
            self.embeddings = self.embed_model.get_embeddings(specs, self.device)

    def _get_seconds_from_time_string(self, time_str):
        time_str = time_str.strip()
        if len(time_str) == 0:
            return None

        seconds = 0
        tokens = time_str.split(':')
        if len(tokens) > 2:
            seconds += 3600 * int(tokens[-3])

        if len(tokens) > 1:
            seconds += 60 * int(tokens[-2])

        seconds += float(tokens[-1])
        return seconds

    # get the list of spectrograms
    def _get_specs(self, start_seconds, end_seconds):
        increment = max(0.5, cfg.audio.segment_len - self.overlap)
        self.offsets = np.arange(start_seconds, end_seconds + 1.0, increment).tolist()
        self.raw_spectrograms = [0 for i in range(len(self.offsets))]
        specs = self.audio.get_spectrograms(self.offsets, segment_len=cfg.audio.segment_len, raw_spectrograms=self.raw_spectrograms)

        spec_array = np.zeros((len(specs), 1, cfg.audio.spec_height, cfg.audio.spec_width))
        for i in range(len(specs)):
            if specs[i] is not None:
                spec_array[i] = specs[i].reshape((1, cfg.audio.spec_height, cfg.audio.spec_width)).astype(np.float32)
            else:
                logging.debug(f"No spectrogram returned for offset {i} ({self.offsets[i]:.2f})")

        return spec_array

    def _analyze_file(self, file_path):
        check_frequency = self.check_frequency
        if check_frequency:
            if self.location_date_dict is not None:
                filename = Path(file_path).name
                if filename in self.location_date_dict:
                    latitude, longitude, self.week_num = self.location_date_dict[filename]
                    if self.week_num is None:
                        check_frequency = False
                    else:
                        county = None
                        for c in self.counties:
                            if latitude >= c.min_y and latitude <= c.max_y and longitude >= c.min_x and longitude <= c.max_x:
                                county = c
                                break

                        if county is None:
                            check_frequency = False
                            logging.warning(f"Warning: no matching county found for latitude={latitude} and longitude={longitude}")
                        else:
                            self._update_class_frequency_stats([county])
                else:
                    # when a filelist is specified, only the recordings in that file are processed;
                    # so you can specify a filelist with no locations or dates if you want to restrict the recording
                    # list but not invoke location/date processing; you still need the standard CSV format
                    # with the expected number of columns, but latitude/longitude/date can be empty
                    if not self.issued_skip_files_warning:
                        logging.info(f"Thread {self.thread_num}: skipping some recordings that were not included in {self.filelist} (e.g. {filename})")
                        self.issued_skip_files_warning = True

                    return
            elif self.get_date_from_file_name:
                result = re.split(cfg.infer.file_date_regex, os.path.basename(file_path))
                if len(result) > cfg.infer.file_date_regex_group:
                    date_str = result[cfg.infer.file_date_regex_group]
                    self.week_num = self._get_week_num_from_date_str(date_str)
                    if self.week_num is None:
                        logging.error(f'Error: invalid date string: {self.date_str} extracted from {file_path}')
                        check_frequency = False # ignore species frequencies for this file

        logging.info(f"Thread {self.thread_num}: Analyzing {file_path}")

        # clear info from previous recording, and mark classes where frequency of eBird reports is too low
        for class_info in self.class_infos:
            class_info.reset()
            if check_frequency and class_info.is_bird and not class_info.ignore:
                if self.week_num is None and not self.get_date_from_file_name:
                    if class_info.max_frequency < cfg.infer.min_location_freq:
                        class_info.ebird_frequency_too_low = True
                elif class_info.frequency[self.week_num - 1] < cfg.infer.min_location_freq:
                    class_info.ebird_frequency_too_low = True

        signal, rate = self.audio.load(file_path)

        if not self.audio.have_signal:
            return

        self._get_predictions(signal, rate)

        # do pre-processing for individual species
        self.species_handlers.reset(self.class_infos, self.offsets, self.raw_spectrograms, self.audio, self.check_frequency, self.week_num)
        for class_info in self.class_infos:
            if  not class_info.ignore and class_info.code in self.species_handlers.handlers:
                self.species_handlers.handlers[class_info.code](class_info)

        # generate labels for one class at a time
        labels = []
        rarities_labels = []
        for class_info in self.class_infos:
            if class_info.ignore or not class_info.has_label:
                continue

            if cfg.infer.use_banding_codes:
                name = class_info.code
            else:
                name = class_info.name

            # set is_label[i] = True for any offset that qualifies in a first pass
            scores = class_info.scores
            for i in range(len(scores)):
                if scores[i] < cfg.infer.min_score or scores[i] == 0: # check for -p 0 case
                    continue

                class_info.is_label[i] = True

            # raise scores if the species' presence is confirmed
            if cfg.infer.lower_min_if_confirmed and cfg.infer.min_score > 0:
                # calculate number of seconds labelled so far
                seconds = 0
                raised_min_score = cfg.infer.min_score + cfg.infer.raise_min_to_confirm * (1 - cfg.infer.min_score)
                for i in range(len(class_info.is_label)):
                    if class_info.is_label[i] and scores[i] >= raised_min_score:
                        if i > 0 and class_info.is_label[i - 1]:
                            seconds += self.overlap
                        else:
                            seconds += cfg.audio.segment_len

                if seconds > cfg.infer.confirmed_if_seconds:
                    # species presence is considered confirmed, so lower the min score and scan again
                    lowered_min_score = cfg.infer.lower_min_factor * cfg.infer.min_score
                    for i in range(len(scores)):
                        if not class_info.is_label[i] and scores[i] >= lowered_min_score:
                            class_info.is_label[i] = True
                            scores[i] = cfg.infer.min_score # display it as min_score in the label

            # generate the labels
            prev_label = None
            for i in range(len(scores)):
                if class_info.is_label[i]:
                    end_time = self.offsets[i] + cfg.audio.segment_len
                    if self.merge_labels and prev_label != None and prev_label.end_time >= self.offsets[i]:
                        # extend the previous label's end time (i.e. merge)
                        prev_label.end_time = end_time
                        prev_label.score = max(scores[i], prev_label.score)
                    else:
                        label = Label(name, scores[i], self.offsets[i], end_time)

                        if class_info.ebird_frequency_too_low:
                            rarities_labels.append(label)
                        else:
                            labels.append(label)

                        prev_label = label
    
        self._save_labels(labels, file_path, False)
        self._save_labels(rarities_labels, file_path, True)
        if self.embed:
            self._save_embeddings(file_path)
        
        # My code to return a data.frame ------------------------------------------------
        labels_df_list = []
        for label_i in labels:
            label_df_i  = pd.DataFrame(
                {
                    # 'filename' : 
                    'start_time' : [label_i.start_time],
                    'end_time' : [label_i.end_time], 
                    'label' :  [label_i.class_name],
                    'score' : [label_i.score],
                })
            labels_df_list.append(label_df_i)
        
        df = pd.concat(labels_df_list)
        
        return df

    def _save_labels(self, labels, file_path, rarities):
        if rarities:
            if len(labels) == 0:
                return # don't write to rarities if none for this species

            if not self.have_rarities_directory and not os.path.exists(self.rarities_output_path):
                os.makedirs(self.rarities_output_path)
                self.have_rarities_directory = True

            output_path = os.path.join(self.rarities_output_path, f'{Path(file_path).stem}_HawkEars.txt')
        else:
            output_path = os.path.join(self.output_path, f'{Path(file_path).stem}_HawkEars.txt')

        logging.info(f"Thread {self.thread_num}: Writing {output_path}")
        try:
            with open(output_path, 'w') as file:
                for label in labels:
                    file.write(f'{label.start_time:.2f}\t{label.end_time:.2f}\t{label.class_name};{label.score:.3f}\n')

                    if self.embed and not rarities:
                        # save offsets with labels for use when saving embeddings
                        self.offsets_with_labels = {}
                        curr_time = label.start_time
                        self.offsets_with_labels[label.start_time] = 1
                        while abs(label.end_time - curr_time - cfg.audio.segment_len) > .001:
                            if self.overlap > 0:
                                curr_time += self.overlap
                            else:
                                curr_time += cfg.audio.segment_len

                            self.offsets_with_labels[curr_time] = 1
        except:
            logging.error(f"Unable to write file {output_path}")
            quit()

    def _save_embeddings(self, file_path):
        embedding_list = []

        if cfg.infer.all_embeddings:
            for i in range(len(self.embeddings)):
                embedding_list.append([self.offsets[i], zlib.compress(self.embeddings[i])])
        else:
            # save embeddings for offsets with labels only
            for offset in sorted(list(self.offsets_with_labels.keys())):
                embedding_list.append([offset, zlib.compress(self.embeddings[int(offset / self.overlap)])])

        output_path = os.path.join(self.output_path, f'{Path(file_path).stem}_HawkEars_embeddings.pickle')
        logging.info(f"Thread {self.thread_num}: Writing {output_path}")
        pickle_file = open(output_path, 'wb')
        pickle.dump(embedding_list, pickle_file)

    # in debug mode, output the top predictions for the first segment
    def _log_predictions(self, predictions):
        predictions = np.copy(predictions[0])
        sum = predictions.sum()
        logging.info("")
        logging.info("Top predictions:")

        for i in range(cfg.infer.top_n):
            j = np.argmax(predictions)
            code = self.class_infos[j].code
            score = predictions[j]
            logging.info(f"{code}: {score}")
            predictions[j] = 0

        logging.info(f"Sum={sum}")
        logging.info("")

    def run(self, file_list, results):
        torch.cuda.empty_cache()
        model_paths = glob.glob(os.path.join(cfg.misc.main_ckpt_folder, "*.ckpt"))
        if len(model_paths) == 0:
            logging.error(f"Error: no checkpoints found in {cfg.misc.main_ckpt_folder}")
            quit()

        self.models = []
        for model_path in model_paths:
            model = main_model.MainModel.load_from_checkpoint(model_path, map_location=torch.device(self.device))
            model.eval() # set inference mode
            self.models.append(model)

        if self.embed:
            self.embed_model = main_model.MainModel.load_from_checkpoint(cfg.misc.search_ckpt_path, map_location=torch.device(self.device))
            self.embed_model.eval()

        self.audio = audio.Audio(device=self.device)
        self.class_infos = self._get_class_infos()
        self._process_location_and_date()
        self.species_handlers = species_handlers.Species_Handlers(self.device)
        
        labels_df_list = [] 
        for file_path in file_list:
            label_df_i = self._analyze_file(file_path)
            label_df_i['filename'] = file_path
            labels_df_list.append(label_df_i)
        
        labels_df = pd.concat(labels_df_list)
        
        # if isinstance(results, mp.Queue):  # For multiprocessing
        if type(results) is mp.queues.Queue:
            results.put(labels_df)
        else:  # For threading
            results.append(labels_df)
        
        return labels_df

if __name__ == '__main__':
    # command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('-b', '--band', type=int, default=1 * cfg.infer.use_banding_codes, help=f"If 1, use banding codes labels. If 0, use common names. Default = {1 * cfg.infer.use_banding_codes}.")
    parser.add_argument('-d', '--debug', default=False, action='store_true', help='Flag for debug mode (analyze one spectrogram only, and output several top candidates).')
    parser.add_argument('--embed', default=False, action='store_true', help='If specified, generate a pickle file containing embeddings for each recording processed.')
    parser.add_argument('-e', '--end', type=str, default='', help="Optional end time in hh:mm:ss format, where hh and mm are optional.")
    parser.add_argument('-i', '--input', type=str, default='', help="Input path (single audio file or directory). No default.")
    parser.add_argument('-o', '--output', type=str, default='', help="Output directory to contain label files. Default is input path, if that is a directory.")
    parser.add_argument('--overlap', type=float, default=cfg.infer.spec_overlap_seconds, help=f"Seconds of overlap for adjacent 3-second spectrograms. Default = {cfg.infer.spec_overlap_seconds}.")
    parser.add_argument('-m', '--merge', type=int, default=1, help=f'Specify 0 to not merge adjacent labels of same species. Default = 1, i.e. merge.')
    parser.add_argument('-p', '--min_score', type=float, default=cfg.infer.min_score, help=f"Generate label if score >= this. Default = {cfg.infer.min_score}.")
    parser.add_argument('-s', '--start', type=str, default='', help="Optional start time in hh:mm:ss format, where hh and mm are optional.")
    parser.add_argument('--threads', type=int, default=cfg.infer.num_threads, help=f'Number of threads. Default = {cfg.infer.num_threads}')
    parser.add_argument('--power', type=float, default=cfg.infer.audio_exponent, help=f'Power parameter to mel spectrograms. Default = {cfg.infer.audio_exponent}')

    # arguments for location/date processing
    parser.add_argument('--date', type=str, default=None, help=f'Date in yyyymmdd, mmdd, or file. Specifying file extracts the date from the file name, using the file_date_regex in base_config.py.')
    parser.add_argument('--lat', type=float, default=None, help=f'Latitude. Use with longitude to identify an eBird county and ignore corresponding rarities.')
    parser.add_argument('--lon', type=float, default=None, help=f'Longitude. Use with latitude to identify an eBird county and ignore corresponding rarities.')
    parser.add_argument('--filelist', type=str, default=None, help=f'Path to optional CSV file containing input file names, latitudes, longitudes and recording dates.')
    parser.add_argument('--region', type=str, default=None, help=f'eBird region code, e.g. "CA-AB" for Alberta. Use as an alternative to latitude/longitude.')

    # arguments for low-pass, high-pass and band-pass filters
    parser.add_argument('--unfilt', type=int, default=cfg.infer.do_unfiltered, help=f'Specify 0 to omit unfiltered inference when using filters. If set to 1, use max of filtered and unfiltered predictions (default = {cfg.infer.do_unfiltered}).')
    parser.add_argument('--lpf', type=int, default=cfg.infer.do_lpf, help=f'Specify 1 to enable low-pass filter (default = {cfg.infer.do_lpf}).')
    parser.add_argument('--lpfstart', type=int, default=cfg.infer.lpf_start_freq, help=f'Start frequency for low-pass filter curve (default = {cfg.infer.lpf_start_freq}).')
    parser.add_argument('--lpfend', type=int, default=cfg.infer.lpf_end_freq, help=f'End frequency for low-pass filter curve (default = {cfg.infer.lpf_end_freq}).')
    parser.add_argument('--lpfdamp', type=float, default=cfg.infer.lpf_damp, help=f'Amount of damping from 0 to 1 for low-pass filter (default = {cfg.infer.lpf_damp}).')
    parser.add_argument('--hpf', type=int, default=cfg.infer.do_hpf, help=f'Specify 1 to enable high-pass filter (default = {cfg.infer.do_hpf}).')
    parser.add_argument('--hpfstart', type=int, default=cfg.infer.hpf_start_freq, help=f'Start frequency for high-pass filter curve (default = {cfg.infer.hpf_start_freq}).')
    parser.add_argument('--hpfend', type=int, default=cfg.infer.hpf_end_freq, help=f'End frequency for high-pass filter curve (default = {cfg.infer.hpf_end_freq}).')
    parser.add_argument('--hpfdamp', type=float, default=cfg.infer.hpf_damp, help=f'Amount of damping from 0 to 1 for high-pass filter (default = {cfg.infer.hpf_damp}).')
    parser.add_argument('--bpf', type=int, default=cfg.infer.do_bpf, help=f'Specify 1 to enable band-pass filter (default = {cfg.infer.do_bpf}).')
    parser.add_argument('--bpfstart', type=int, default=cfg.infer.bpf_start_freq, help=f'Start frequency for band-pass filter curve (default = {cfg.infer.bpf_start_freq}).')
    parser.add_argument('--bpfend', type=int, default=cfg.infer.bpf_end_freq, help=f'End frequency for band-pass filter curve (default = {cfg.infer.bpf_end_freq}).')
    parser.add_argument('--bpfdamp', type=float, default=cfg.infer.bpf_damp, help=f'Amount of damping from 0 to 1 for band-pass filter (default = {cfg.infer.bpf_damp}).')

    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level, format='%(asctime)s.%(msecs)03d %(message)s', datefmt='%H:%M:%S')
    start_time = time.time()
    logging.info("Initializing")

    num_threads = args.threads
    cfg.infer.use_banding_codes = args.band
    cfg.audio.power = args.power
    cfg.infer.min_score = args.min_score
    if cfg.infer.min_score < 0:
        logging.error("Error: min_score must be >= 0")
        quit()

    if torch.cuda.is_available():
        device = 'cuda'
        logging.info(f"Using GPU")
    else:
        # TODO: use openvino to improve performance when no GPU is available
        device = 'cpu'
        logging.info(f"Using CPU")

    cfg.infer.do_unfiltered = args.unfilt
    cfg.infer.do_lpf = args.lpf
    cfg.infer.lpf_start_freq = args.lpfstart
    cfg.infer.lpf_end_freq = args.lpfend
    cfg.infer.lpf_damp = args.lpfdamp

    cfg.infer.do_hpf = args.hpf
    cfg.infer.hpf_start_freq = args.hpfstart
    cfg.infer.hpf_end_freq = args.hpfend
    cfg.infer.hpf_damp = args.hpfdamp

    cfg.infer.do_bpf = args.bpf
    cfg.infer.bpf_start_freq = args.bpfstart
    cfg.infer.bpf_end_freq = args.bpfend
    cfg.infer.bpf_damp = args.bpfdamp

    file_list = Analyzer._get_file_list(args.input)
    # file_list = glob.glob('/Users/lviotti/Library/CloudStorage/Dropbox/Work/Kitzes/datasets/yera2023osu/**/*.WAV', recursive = True)
    # file_list = glob.glob(os.path.join(args.input, "**/*.WAV"), recursive = True)
    
    # Initialize the shared Queue (for multiprocessing) or list (for threading) to collect results
    results = mp.Queue() if os.name == "posix" else []
    
    if num_threads == 1:
        # keep it simple in case multithreading code has undesirable side-effects (e.g. disabling echo to terminal)
        analyzer = Analyzer(args.input, args.output, args.start, args.end, args.date, args.lat, args.lon, args.region,
                            args.filelist, args.debug, args.merge, args.overlap, device, 1, args.embed)
        final_df = analyzer.run(file_list, results)
    else:
        # split input files into one group per thread
        file_lists = [[] for i in range(num_threads)]
        for i in range(len(file_list)):
            file_lists[i % num_threads].append(file_list[i])

        # for some reason using processes is faster than just using threads, but that disables output on Windows
        processes = []
        for i in range(num_threads):
            if len(file_lists[i]) > 0:
                analyzer = Analyzer(args.input, args.output, args.start, args.end, args.date, args.lat, args.lon, args.region,
                                    args.filelist, args.debug, args.merge, args.overlap, device, i + 1, args.embed)
                if os.name == "posix":
                    process = mp.Process(target=analyzer.run, args=(file_lists[i], results))
                else:
                    process = threading.Thread(target=analyzer.run, args=(file_lists[i], results))

                process.start()
                processes.append(process)

        # wait for processes to complete
        for process in processes:
            try:
                process.join()
            except Exception as e:
                logging.error(f"Caught exception: {e}")
        
        # Retrieve DataFrames from results
        dataframes = []
        if os.name == "posix":
            while not results.empty():
                dataframes.append(results.get())
        else:
            dataframes = results  # Results 
        
        # Combine all DataFrames if needed
        final_df = pd.concat(dataframes, ignore_index=True)
        
    if os.name == "posix":
        os.system("stty echo")

    
    #----------------------------------------------------------------------------------------------
    # Process df
    
    # final_df
    
    # Filter selected class
    # species_df = final_df[final_df['label'] == 'WISN']
    species_df = final_df[final_df['label'] == 'YERA']
    
    # Reshape to wide format with one-hot encoding for 'label'
    wide_df = species_df.pivot_table(index=['start_time', 'end_time', 'filename'],
                            columns='label', values='score').fillna(0).reset_index()
    
    # out_path = args.input
    out_path = args.output
    suffix =  out_path.split('/')[-1]
    today = time.strftime("%Y-%m-%d")
    csv_file_name = os.path.join(out_path, f'predictions-yera-{suffix}-{today}.csv')
    wide_df.to_csv(csv_file_name, index = False)
    
    #----------------------------------------------------------------------------------------------
    elapsed = time.time() - start_time
    minutes = int(elapsed) // 60
    seconds = int(elapsed) % 60
    logging.info(f"Elapsed time = {minutes}m {seconds}s")
