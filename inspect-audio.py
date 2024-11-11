import pandas as pd
import numpy as np
import argparse, time, os
from glob import glob
from opensoundscape import Audio


# python inspect-audio.py /Users/lviotti/Library/CloudStorage/Dropbox/Work/Kitzes/datasets/yera2023osu -o /Users/lviotti/Library/CloudStorage/Dropbox/Work/Kitzes/projects/marsh-yera

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=str, help = 'Path to folder containing audio data')
    parser.add_argument('-o', '--output', type=str, default='', help="Output directory")
    
    return parser.parse_args()


#-------------------------------------------------------------------------------------
if __name__ == "__main__":
    args = parse_args()
    
    # Benchmarking
    start = time.time()
    
    # Create dataframe ---------------------------------------------------------------
    file_list = glob(os.path.join(args.input, "**/*.WAV"), recursive = True)
    
    df = pd.DataFrame({'file':file_list})
    
    # Folder columns
    df['dir'] = df['file'].str.split('/', expand=True).iloc[:, -3]
    df['subdir'] = df['file'].str.split('/', expand=True).iloc[:, -2]
    
    # Test if files are corrupted ----------------------------------------------------
    
    df['loaded'] = np.NaN
    
    for idx,row in df.iterrows():
        # print(row)
        try:
            audio = Audio.from_file(row['file'], duration=1)
            df['loaded'].iloc[idx] = 1
        except:
            df['loaded'].iloc[idx] = 0
    
    # Export -------------------------------------------------------------------------
    if args.output:
        today = time.strftime("%Y-%m-%d")
        folder = os.path.basename(args.input)
        df.to_csv(os.path.join(args.output, f'{folder}-files.csv'), index=False)
        
    # Bechnarking --------------------------------------------------------------------
    end = time.time()
    total_time = round(end - start)
    print(f'Total time: {round(total_time/60)}m  {total_time % 60}s ' )
    
    
    
    