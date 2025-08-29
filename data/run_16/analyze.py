import numpy as np
import os
import argparse
from tqdm import tqdm

from tamalero.DataFrame import DataFrame
from tamalero.FIFO import merge_words

def analyze_binary_file(file_path):

    if not os.path.exists(file_path):
        print(f"Error '{file_path}' ")
        return

    print(f"--- Analyzing file: {os.path.basename(file_path)} ---")

    try:
        raw_data_32bit = np.fromfile(file_path, dtype=np.uint32)
        print(f"Successful read {len(raw_data_32bit)} 32 bits。")
    except Exception as e:
        print(f"Reading error: {e}")
        return

    df = DataFrame(version='ETROC2')

    merged_data_64bit = merge_words(raw_data_32bit.tolist())
    print(f"merge into {len(merged_data_64bit)} 64 bit。")

    parsed_events = [df.read(word) for word in tqdm(merged_data_64bit, desc="decode")]

    header_count = 0
    trailer_count = 0
    hit_count = 0
    pixel_hits = {}
    elink_hits = {}

    for event_type, event_data in parsed_events:
        if not event_type:
            continue
        
        if event_type == 'header':
            header_count += 1
        elif event_type == 'trailer':
            trailer_count += 1
        elif event_type == 'data':
            hit_count += 1
            elink = event_data.get('elink', 'N/A')
            row = event_data.get('row_id', 'N/A')
            col = event_data.get('col_id', 'N/A')
            
            elink_hits[elink] = elink_hits.get(elink, 0) + 1
            pixel_key = f"({row},{col})"
            pixel_hits[pixel_key] = pixel_hits.get(pixel_key, 0) + 1

    print("\n--- Analyze report ---")
    print(f"  Headers: {header_count}")
    print(f"  Trailers: {trailer_count}")
    print(f"  Cosmic hits: {hit_count}")
    
    print("\n--- Elink hits report ---")
    for elink, count in sorted(elink_hits.items()):
        print(f"  Elink {elink}: {count} hits")
        
    print("\n--- Top 10 pixel report ---")
    sorted_pixels = sorted(pixel_hits.items(), key=lambda item: item[1], reverse=True)
    for i, (pixel, count) in enumerate(sorted_pixels[:10]):
        print(f"  {i+1}. Pixel {pixel}: {count} hits")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze data from Constellation DAQ original .bin file")
    parser.add_argument("file", help="path of .bin file。")
    args = parser.parse_args()
    
    analyze_binary_file(args.file)