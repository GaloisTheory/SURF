"""Stratified subsample of Dolci dataset, proportional to sub-dataset sizes."""

from datasets import load_dataset

OUTPUT_PATH = "data/dolci-25k-sampled"
NUM_SAMPLES = 25000
SEED = 42

print(f"Loading Dolci-Think-SFT-7B dataset...")
ds = load_dataset("allenai/Dolci-Think-SFT-7B", split="train")
print(f"Full dataset: {len(ds):,} rows")

print(f"Shuffling with seed={SEED}...")
ds = ds.shuffle(seed=SEED)

print(f"Selecting first {NUM_SAMPLES:,} rows...")
ds = ds.select(range(NUM_SAMPLES))

print(f"Saving to {OUTPUT_PATH}...")
ds.save_to_disk(OUTPUT_PATH)
print(f"Done. Saved {len(ds):,} rows to {OUTPUT_PATH}")
