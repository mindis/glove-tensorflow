import json
import shutil
from argparse import ArgumentParser

import tensorflow as tf

from trainer.utils import (get_optimizer,
                           get_input_fn,
                           get_serving_input_fn,
                           get_run_config,
                           get_train_spec,
                           get_exporter,
                           get_eval_spec)
from trainer.text8 import COL_DEFAULTS, COL_NAMES, LABEL_COL


def get_field_variables(features, field_variables, embedding_size=64):
    # create field variables
    field_id_lookup = (tf.contrib.lookup
                       .index_table_from_tensor(field_variables["mapping"], default_value=0,
                                                name=field_variables["name"] + "_id_lookup"))
    field_dim = len(field_variables["mapping"])
    field_embeddings = tf.get_variable(field_variables["name"] + "_embeddings", [field_dim, embedding_size])
    # [field_dim, embedding_size]
    field_biases = tf.get_variable(field_variables["name"] + "_biases", [field_dim])
    # [field_dim]
    tf.summary.histogram(field_variables["name"] + "_bias", field_biases)

    # get field values
    field_id = field_id_lookup.lookup(features[field_variables["name"]])
    # [None]
    field_embed = tf.nn.embedding_lookup(field_embeddings, field_id,
                                         name=field_variables["name"] + "_embed_lookup")
    # [None, embedding_size]
    field_bias = tf.nn.embedding_lookup(field_biases, field_id,
                                        name=field_variables["name"] + "_bias_lookup")
    # [None, 1]

    field_variables.update({
        "embeddings": field_embeddings,
        "biases": field_biases,
        "embed": field_embed,
        "bias": field_bias
    })
    return field_variables


def get_similarity(field_variables, k=100):
    field_embed_norm = tf.math.l2_normalize(field_variables["embed"], 1)
    # [None, embedding_size]
    field_embeddings_norm = tf.math.l2_normalize(field_variables["embeddings"], 1)
    # [vocab_size, embedding_size]
    field_cosine_sim = tf.matmul(field_embed_norm, field_embeddings_norm, transpose_b=True)
    # [None, mapping_size]
    field_top_k_sim, field_top_k_idx = tf.math.top_k(field_cosine_sim, k=k,
                                                     name="top_k_sim_" + field_variables["name"])
    # [None, k], [None, k]

    field_string_lookup = (tf.contrib.lookup
                           .index_to_string_table_from_tensor(field_variables["mapping"],
                                                              name=field_variables["name"] + "_string_lookup"))
    field_top_k_string = field_string_lookup.lookup(tf.cast(field_top_k_idx, tf.int64))
    # [None, k]
    field_variables.update({
        "embed_norm": field_embed_norm,
        "embeddings_norm": field_embeddings_norm,
        "top_k_sim": field_top_k_sim,
        "top_k_idx": field_top_k_idx,
        "top_k_string": field_top_k_string
    })
    return field_variables


def model_fn(features, labels, mode, params):
    field_names = params.get("field_names",
                             {
                                 "row_id": "row_id",
                                 "column_id": "column_id",
                                 "weight": "weight",
                                 "value": "value",
                             })
    mappings = params["mappings"]
    embedding_size = params.get("embedding_size", 64)
    optimizer_name = params.get("optimizer", "Adam")
    learning_rate = params.get("learning_rate", 0.001)
    k = params.get("k", 100)

    row_id_variables = {"name": field_names["row_id"], "mapping": mappings[field_names["row_id"]]}
    column_id_variables = {"name": field_names["column_id"], "mapping": mappings[field_names["column_id"]]}

    with tf.name_scope("mf"):
        # global bias
        global_bias = tf.get_variable("global_bias", [])
        # []
        tf.summary.scalar("global_bias", global_bias)
        # row mapping, embeddings and biases
        row_id_variables = get_field_variables(features, row_id_variables, embedding_size)
        # column mapping, embeddings and biases
        column_id_variables = get_field_variables(features, column_id_variables, embedding_size)

        # matrix factorisation
        embed_product = tf.reduce_sum(tf.multiply(row_id_variables["embed"], column_id_variables["embed"]), 1)
        # [None, 1]
        predicted_value = tf.add(global_bias,
                                 tf.add_n([row_id_variables["bias"],
                                           column_id_variables["bias"],
                                           embed_product]))
        # [None, 1]

    # prediction
    if mode == tf.estimator.ModeKeys.PREDICT:
        # calculate similarity
        with tf.name_scope("similarity"):
            # row similarity
            row_id_variables = get_similarity(row_id_variables, k)
            # column similarity
            column_id_variables = get_similarity(column_id_variables, k)

            embed_norm_product = tf.reduce_sum(tf.multiply(row_id_variables["embed_norm"],
                                                           column_id_variables["embed_norm"]), 1)

        predictions = {
            "row_embed": row_id_variables["embed"],
            "row_bias": row_id_variables["bias"],
            "column_embed": column_id_variables["embed"],
            "column_bias": column_id_variables["bias"],
            "embed_norm_product": embed_norm_product,
            "top_k_row_similarity": row_id_variables["top_k_sim"],
            "top_k_row_string": row_id_variables["top_k_string"],
            "top_k_column_similarity": column_id_variables["top_k_sim"],
            "top_k_column_string": column_id_variables["top_k_string"]
        }
        return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions)

    # evaluation
    with tf.name_scope("mse"):
        loss = tf.losses.mean_squared_error(features[field_names["value"]],
                                            predicted_value,
                                            features[field_names["weight"]])
        # []
    if mode == tf.estimator.ModeKeys.EVAL:
        return tf.estimator.EstimatorSpec(mode=mode, loss=loss)

    # training
    with tf.name_scope("train"):
        optimizer = get_optimizer(optimizer_name, learning_rate)
        train_op = optimizer.minimize(loss, global_step=tf.train.get_global_step())
    if mode == tf.estimator.ModeKeys.TRAIN:
        return tf.estimator.EstimatorSpec(mode=mode, loss=loss, train_op=train_op)


def train_and_evaluate(args):
    # paths
    train_csv = args.train_csv
    vocab_json = args.vocab_json
    job_dir = args.job_dir
    restore = args.restore
    # model
    embedding_size = args.embedding_size
    k = args.k
    # training
    batch_size = args.batch_size
    train_steps = args.train_steps

    # init
    tf.logging.set_verbosity(tf.logging.INFO)
    if not restore:
        shutil.rmtree(job_dir, ignore_errors=True)

    # load vocab
    with open(vocab_json) as f:
        vocab = json.load(f)

    # estimator
    run_config = get_run_config()
    estimator = tf.estimator.Estimator(
        model_fn=model_fn,
        model_dir=job_dir,
        config=run_config,
        params={
            "field_names": {
                "row_id": "row_token",
                "column_id": "column_token",
                "weight": "glove_weight",
                "value": "glove_value",
            },
            "mappings": {
                "row_token": vocab,
                "column_token": vocab,
            },
            "embedding_size": embedding_size,
            "k": k,
        }
    )

    # train spec
    text8_args = {"col_names": COL_NAMES, "col_defaults": COL_DEFAULTS, "label_col": LABEL_COL}
    train_input_fn = get_input_fn(train_csv, batch_size=batch_size, **text8_args)
    train_spec = get_train_spec(train_input_fn, train_steps)

    # eval spec
    eval_input_fn = get_input_fn(train_csv, mode=tf.estimator.ModeKeys.EVAL, batch_size=batch_size, **text8_args)
    exporter = get_exporter(get_serving_input_fn())
    eval_spec = get_eval_spec(eval_input_fn, exporter)

    # train and evaluate
    tf.estimator.train_and_evaluate(estimator, train_spec, eval_spec)


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument("--train-csv", default="data/interaction.csv",
                        help="path to the training csv data (default: %(default)s)")
    parser.add_argument("--vocab-json", default="data/vocab.json",
                        help="path to the vocab json (default: %(default)s)")
    parser.add_argument("--job-dir", default="checkpoints/glove",
                        help="job directory (default: %(default)s)")
    parser.add_argument("--restore", action="store_true",
                        help="whether to restore from JOB_DIR")
    parser.add_argument("--embedding-size", type=int, default=64,
                        help="embedding size (default: %(default)s)")
    parser.add_argument("--k", type=int, default=100,
                        help="k for top k similarity (default: %(default)s)")
    parser.add_argument("--batch-size", type=int, default=1024,
                        help="batch size (default: %(default)s)")
    parser.add_argument("--train-steps", type=int, default=20000,
                        help="number of training steps (default: %(default)s)")
    args = parser.parse_args()

    train_and_evaluate(args)
