import tiledb
import pandas as pd
import numpy as np
import cv2
import boto3
import time

s3 = boto3.client('s3')

bucket = 'tiledb-gskoumas'
prefix = 'airbus_ship_detection/train_v2'

BATCH_SIZE = 32

IMAGE_SHAPE = (64, 64)


def chunks(lst, n=BATCH_SIZE):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n], (i, i + n)


print('Loading training segments csv to df...')
df = pd.read_csv('train_ship_segmentations_v2.csv', usecols=['ImageId', 'EncodedPixels'])

not_empty_boolean = pd.notna(df['EncodedPixels'])
unique_non_empty_images = df[not_empty_boolean].ImageId.nunique()
empty_images = (~not_empty_boolean).sum()

df['has_ship'] = df['EncodedPixels'].map(lambda x: True if type(x) is str else False)

grouped_df = df.groupby('has_ship')

df = pd.concat([grouped_df.get_group(True), grouped_df.get_group(False).sample(n=unique_non_empty_images)])

# Drop not needed column
df = df.drop('EncodedPixels', axis=1)

# Shuffle dataframe
df = df.sample(frac=1)

# Split in train and validation
print('Split into train and validation')
split_msk = np.random.rand(len(df)) < 0.9

train_df = df[split_msk]
val_df = df[~split_msk]

train_labels = [1.0 if label is True else 0.0 for label in train_df['has_ship']]
val_labels = [1.0 if label is True else 0.0 for label in val_df['has_ship']]

train_labels = np.array(train_labels, dtype=np.float32)
val_labels = np.array(val_labels, dtype=np.float32)

# Ingestion
print('Creating training segments TileDB array...')

ctx = tiledb.Ctx()

dom = tiledb.Domain(
    tiledb.Dim(name="label_id", domain=(0, len(train_df) - 1), tile=BATCH_SIZE, dtype=np.int32),
)

attrs = [
    tiledb.Attr(name="label", dtype=np.float32,)
]

schema = tiledb.ArraySchema(domain=dom, sparse=False, attrs=attrs)

array = "s3://tiledb-gskoumas/airbus_ship_detection_tiledb/train_ship_segments"

tiledb.Array.create(array, schema)

print('Ingesting training segments to TileDB...')
with tiledb.open(array, 'w') as array:
    array[:] = {"label": train_labels}

print('Creating validation segments TileDB array...')
dom = tiledb.Domain(
    tiledb.Dim(name="label_id", domain=(0, len(val_df) - 1), tile=BATCH_SIZE, dtype=np.int32),
)

attrs = [
    tiledb.Attr(name="label", dtype=np.float32,)
]

schema = tiledb.ArraySchema(domain=dom, sparse=False, attrs=attrs)

array = "s3://tiledb-gskoumas/airbus_ship_detection_tiledb/val_ship_segments"

tiledb.Array.create(array, schema)

print('Ingesting validation segments to TileDB...')

with tiledb.open(array, 'w', ctx=ctx) as array:
     array[:] = {"label": val_labels}

print('Done with segments!')


print('Ingestion of training images...')
dom_image_train = tiledb.Domain(
    tiledb.Dim(name="image_id", domain=(0, len(train_df) - 1), tile=BATCH_SIZE, dtype=np.int32),
    tiledb.Dim(name="x_axis", domain=(0, 128 - 1), tile=128, dtype=np.int32),
    tiledb.Dim(name="y_axis", domain=(0, 128 - 1), tile=128, dtype=np.int32),
    ctx=ctx,
)

attrs = [
        tiledb.Attr(name="rgb", dtype=[("", np.float32), ("", np.float32), ("", np.float32)], var=False,
                    filters=tiledb.FilterList([tiledb.ZstdFilter(level=6)])),
    ]

schema = tiledb.ArraySchema(domain=dom_image_train,
                            sparse=False,
                            attrs=attrs,
                            cell_order='row-major',
                            tile_order='row-major',
                            capacity=10000,
                            ctx=ctx)

array = "s3://tiledb-gskoumas/airbus_ship_detection_tiledb/train_ship_images"

tiledb.Array.create(array, schema)

image_chunks = chunks(train_df['ImageId'].tolist())

number_of_chunks = len(train_df['ImageId'].tolist()) // BATCH_SIZE

with tiledb.open(array, 'w', ctx=ctx) as train_images_tiledb:
    counter = 1
    for chunk, tpl in image_chunks:
        print('Working on chunk ' + str(counter) + ' of ' + str(number_of_chunks))
        image_chunk = []
        for image_id in chunk:
            image_path = 'train_v2/' + image_id
            image = cv2.imread(image_path)
            image = cv2.resize(image, IMAGE_SHAPE)
            image_chunk.append(image.astype(np.float32))

        print('Inserting chunk ' + str(counter) + ' of ' + str(number_of_chunks))
        image_chunk = np.stack(image_chunk, axis=0) / 255.0
        view = image_chunk.view([("", np.float32), ("", np.float32), ("", np.float32)])
        start = time.time()
        #train_images_tiledb[tpl[0]:tpl[1]] = {"r": image_chunk[:, :, :, 0], "g": image_chunk[:, :, :, 1], "b": image_chunk[:, :, :, 2]}
        train_images_tiledb[tpl[0]:tpl[1]] = view
        life_taken = time.time() - start
        print('Insertion took ' + str(life_taken) + 'seconds.')
        counter += 1
        del image_chunk


print('Ingestion of validation images...')

dom_image_val = tiledb.Domain(
    tiledb.Dim(name="image_id", domain=(0, len(val_df) - 1), tile=BATCH_SIZE, dtype=np.int32),
    tiledb.Dim(name="x_axis", domain=(0, 128 - 1), tile=128, dtype=np.int32),
    tiledb.Dim(name="y_axis", domain=(0, 128 - 1), tile=128, dtype=np.int32),
    ctx=ctx,
)

schema = tiledb.ArraySchema(domain=dom_image_val,
                            sparse=False,
                            attrs=attrs,
                            cell_order='row-major',
                            tile_order='row-major',
                            capacity=10000,
                            ctx=ctx)

array = "s3://tiledb-gskoumas/airbus_ship_detection_tiledb/val_ship_images"

tiledb.Array.create(array, schema)

image_chunks = chunks(val_df['ImageId'].tolist())

number_of_chunks = len(val_df['ImageId'].tolist()) // BATCH_SIZE

with tiledb.open(array, 'w', ctx=ctx) as val_images_tiledb:
    counter = 1
    for chunk, tpl in image_chunks:
        print('Working on chunk ' + str(counter) + ' of ' + str(number_of_chunks))
        image_chunk = []
        for image_id in chunk:
            image_path = 'train_v2/' + image_id
            image = cv2.imread(image_path)
            image = cv2.resize(image, IMAGE_SHAPE)
            image_chunk.append(image.astype(np.float32))

        print('Inserting chunk ' + str(counter) + ' of ' + str(number_of_chunks))
        image_chunk = np.stack(image_chunk, axis=0)
        view = image_chunk.view([("", np.float32), ("", np.float32), ("", np.float32)])
        start = time.time()
        #val_images_tiledb[tpl[0]:tpl[1]] = {"r": image_chunk[:, :, :, 0], "g": image_chunk[:, :, :, 1], "b": image_chunk[:, :, :, 2]}
        val_images_tiledb[tpl[0]:tpl[1]] = view
        life_taken = time.time() - start
        print('Insertion took ' + str(life_taken) + 'seconds.')
        counter += 1
        del image_chunk

print('Done with image ingestion!')

train_images = "s3://tiledb-gskoumas/airbus_ship_detection_tiledb/train_ship_images"
train_segs = "s3://tiledb-gskoumas/airbus_ship_detection_tiledb/train_ship_segments"

val_images = "s3://tiledb-gskoumas/airbus_ship_detection_tiledb/val_ship_images"
val_segs = "s3://tiledb-gskoumas/airbus_ship_detection_tiledb/val_ship_segments"

# Alteratively, you can create and pass a configuration object
config = tiledb.Config({"sm.consolidation.mode": "fragment_meta"})
ctx = tiledb.Ctx(config)

print('Consolidating train images...')
tiledb.consolidate(train_images, ctx=ctx)
print('Consolidating train images done!')

print('Consolidating train segments...')
tiledb.consolidate(train_segs, ctx=ctx)
print('Consolidating train segments done!')

print('Consolidating val images...')
tiledb.consolidate(val_images, ctx=ctx)
print('Consolidating val images done!')

print('Consolidating val segments...')
tiledb.consolidate(val_segs, ctx=ctx)
print('Consolidating val segments done!')
