import tensorflow as tf
import pdb

def parse_function(filename):
    """ Reads an image from a file, decodes it into a dense tensor, and resizes it
    to a fixed shape """
    image_string = tf.read_file(filename)
    image_decoded = tf.image.decode_jpeg(image_string)
    image_scaled = tf.image.per_image_standardization(image_decoded)
    return image_scaled


def create_tf_dataset(file_names, buffer_size, number_iterations_dataset, batch_size=1):
    """ This function returns a pointer to iterate over the batches of data """
    train_dataset = tf.data.Dataset.from_tensor_slices((file_names))
    train_dataset = train_dataset.map(parse_function)
    train_dataset = train_dataset.repeat(number_iterations_dataset)
    train_dataset = train_dataset.shuffle(buffer_size=buffer_size)
    train_batched_dataset = train_dataset.batch(batch_size)
    train_iterator = train_batched_dataset.make_initializable_iterator()

    return train_iterator.get_next(), train_iterator

def ssd_bboxes_encode_layer(labels,
                           bboxes,
                           anchors_layer,
                           num_classes,
                           positive_threshold=0.5,
                           prior_scaling=[0.1, 0.1, 0.2, 0.2],
                           dtype=tf.float32):
    """Encode groundtruth labels and bounding boxes using SSD anchors from
    one layer.
    Arguments:
      labels: 1D Tensor(int64) containing groundtruth labels;
      bboxes: Nx4 Tensor(float) with bboxes relative coordinates;
      anchors_layer: Numpy array with layer anchors;
      matching_threshold: Threshold for positive match with groundtruth bboxes;
      prior_scaling: Scaling of encoded coordinates.
    Return:
      (target_labels, target_localizations, target_scores): Target Tensors.
    """
    # Anchors coordinates and volume.
    yref, xref, href, wref = anchors_layer
    ymin = yref - href / 2.
    xmin = xref - wref / 2.
    ymax = yref + href / 2.
    xmax = xref + wref / 2.
    vol_anchors = (xmax - xmin) * (ymax - ymin)

    # Initialize tensors...
    shape = (yref.shape[0], yref.shape[1], href.size)
    feat_labels = tf.zeros(shape, dtype=tf.int64)
    feat_scores = tf.zeros(shape, dtype=dtype)

    feat_ymin = tf.zeros(shape, dtype=dtype)
    feat_xmin = tf.zeros(shape, dtype=dtype)
    feat_ymax = tf.ones(shape, dtype=dtype)
    feat_xmax = tf.ones(shape, dtype=dtype)

    def jaccard_with_anchors(bbox):
        """Compute jaccard score between a box and the anchors.
        """
        int_ymin = tf.maximum(ymin, bbox[0])
        int_xmin = tf.maximum(xmin, bbox[1])
        int_ymax = tf.minimum(ymax, bbox[2])
        int_xmax = tf.minimum(xmax, bbox[3])
        h = tf.maximum(int_ymax - int_ymin, 0.)
        w = tf.maximum(int_xmax - int_xmin, 0.)
        # Volumes
        inter_vol = h * w
        union_vol = vol_anchors - inter_vol \
            + (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        jaccard = tf.div(inter_vol, union_vol)
        return jaccard

    def intersection_with_anchors(bbox):
        """Compute intersection between score a box and the anchors.
        """
        int_ymin = tf.maximum(ymin, bbox[0])
        int_xmin = tf.maximum(xmin, bbox[1])
        int_ymax = tf.minimum(ymax, bbox[2])
        int_xmax = tf.minimum(xmax, bbox[3])
        h = tf.maximum(int_ymax - int_ymin, 0.)
        w = tf.maximum(int_xmax - int_xmin, 0.)
        inter_vol = h * w
        scores = tf.div(inter_vol, vol_anchors)
        return scores

    def condition(i, feat_labels, feat_scores,
                  feat_ymin, feat_xmin, feat_ymax, feat_xmax):
        """Condition: check label index.
        """
        r = tf.less(i, tf.shape(labels))
        return r[0]

    def body(i, feat_labels, feat_scores,
             feat_ymin, feat_xmin, feat_ymax, feat_xmax):
        """Body: update feature labels, scores and bboxes.
        Follow the original SSD paper for that purpose:
          - assign values when jaccard > 0.5;
          - only update if beat the score of other bboxes.
        """
        # Jaccard score
        label = labels[i]
        bbox = bboxes[i]
        jaccard = jaccard_with_anchors(bbox)

        # Mask: check threshold + scores + no annotations + num_classes.
        mask = tf.greater(jaccard, feat_scores)
        mask = tf.logical_and(mask, feat_scores > -0.5)

        # [TODO] : Fix the following line
        mask = tf.logical_and(mask, label < num_classes)
        imask = tf.cast(mask, tf.int64)
        fmask = tf.cast(mask, dtype)
        
        # Update values using mask.
        feat_labels = imask * label + (1 - imask) * feat_labels
        feat_scores = tf.where(mask, jaccard, feat_scores)

        feat_ymin = fmask * bbox[0] + (1 - fmask) * feat_ymin
        feat_xmin = fmask * bbox[1] + (1 - fmask) * feat_xmin
        feat_ymax = fmask * bbox[2] + (1 - fmask) * feat_ymax
        feat_xmax = fmask * bbox[3] + (1 - fmask) * feat_xmax

        return [i+1, feat_labels, feat_scores,
                feat_ymin, feat_xmin, feat_ymax, feat_xmax]

    # Main loop definition.
    i = 0
    [i, feat_labels, feat_scores,
     feat_ymin, feat_xmin,
     feat_ymax, feat_xmax] = tf.while_loop(condition, body,
                                           [i, feat_labels, feat_scores,
                                            feat_ymin, feat_xmin,
                                            feat_ymax, feat_xmax])
    # Transform to center / size.
    feat_cy = (feat_ymax + feat_ymin) / 2.
    feat_cx = (feat_xmax + feat_xmin) / 2.
    feat_h = feat_ymax - feat_ymin
    feat_w = feat_xmax - feat_xmin
    # Encode features.
    feat_cy = (feat_cy - yref) / href / prior_scaling[0]
    feat_cx = (feat_cx - xref) / wref / prior_scaling[1]
    feat_h = tf.log(feat_h / href) / prior_scaling[2]
    feat_w = tf.log(feat_w / wref) / prior_scaling[3]
    # Use SSD ordering: x / y / w / h instead of ours.
    feat_localizations = tf.stack([feat_cx, feat_cy, feat_w, feat_h], axis=-1)
    return feat_labels, feat_localizations, feat_scores

def tf_ssd_bboxes_select_layer(predictions_layer, localizations_layer,
                               select_threshold=None,
                               num_classes=21,
                               ignore_class=0,
                               scope=None):
    """Extract classes, scores and bounding boxes from features in one layer.
    Batch-compatible: inputs are supposed to have batch-type shapes.
    Args:
      predictions_layer: A SSD prediction layer;
      localizations_layer: A SSD localization layer;
      select_threshold: Classification threshold for selecting a box. All boxes
        under the threshold are set to 'zero'. If None, no threshold applied.
    Return:
      d_scores, d_bboxes: Dictionary of scores and bboxes Tensors of
        size Batches X N x 1 | 4. Each key corresponding to a class.
    """
    select_threshold = 0.0 if select_threshold is None else select_threshold
    with tf.name_scope(scope, 'ssd_bboxes_select_layer',
                       [predictions_layer, localizations_layer]):
        # Reshape features: Batches x N x N_labels | 4
        p_shape = tfe.get_shape(predictions_layer)
        predictions_layer = tf.reshape(predictions_layer,
                                       tf.stack([p_shape[0], -1, p_shape[-1]]))
        l_shape = tfe.get_shape(localizations_layer)
        localizations_layer = tf.reshape(localizations_layer,
                                         tf.stack([l_shape[0], -1, l_shape[-1]]))

        d_scores = {}
        d_bboxes = {}
        for c in range(0, num_classes):
            if c != ignore_class:
                # Remove boxes under the threshold.
                scores = predictions_layer[:, :, c]
                fmask = tf.cast(tf.greater_equal(scores, select_threshold), scores.dtype)
                scores = scores * fmask
                bboxes = localizations_layer * tf.expand_dims(fmask, axis=-1)
                # Append to dictionary.
                d_scores[c] = scores
                d_bboxes[c] = bboxes

        return d_scores, d_bboxes


def tf_ssd_bboxes_select(predictions_net, localizations_net,
                         select_threshold=None,
                         num_classes=2,
                         ignore_class=0,
                         scope=None):
    """Extract classes, scores and bounding boxes from network output layers.
    Batch-compatible: inputs are supposed to have batch-type shapes.
    Args:
      predictions_net: List of SSD prediction layers;
      localizations_net: List of localization layers;
      select_threshold: Classification threshold for selecting a box. All boxes
        under the threshold are set to 'zero'. If None, no threshold applied.
    Return:
      d_scores, d_bboxes: Dictionary of scores and bboxes Tensors of
        size Batches X N x 1 | 4. Each key corresponding to a class.
    """
    with tf.name_scope(scope, 'ssd_bboxes_select',
                       [predictions_net, localizations_net]):
        l_scores = []
        l_bboxes = []
        for i in range(len(predictions_net)):
            scores, bboxes = tf_ssd_bboxes_select_layer(predictions_net[i][0],
                                                        localizations_net[i],
                                                        select_threshold,
                                                        num_classes,
                                                        ignore_class)
            l_scores.append(scores)
            l_bboxes.append(bboxes)
        # Concat results.
        d_scores = {}
        d_bboxes = {}
        for c in l_scores[0].keys():
            ls = [s[c] for s in l_scores]
            lb = [b[c] for b in l_bboxes]
            d_scores[c] = tf.concat(ls, axis=1)
            d_bboxes[c] = tf.concat(lb, axis=1)
        return d_scores, d_bboxes

def bboxes_sort(scores, bboxes, top_k=400, scope=None):
    """Sort bounding boxes by decreasing order and keep only the top_k.
    If inputs are dictionnaries, assume every key is a different class.
    Assume a batch-type input.
    Args:
      scores: Batch x N Tensor/Dictionary containing float scores.
      bboxes: Batch x N x 4 Tensor/Dictionary containing boxes coordinates.
      top_k: Top_k boxes to keep.
    Return:
      scores, bboxes: Sorted Tensors/Dictionaries of shape Batch x Top_k x 1|4.
    """
    # Dictionaries as inputs.
    if isinstance(scores, dict) or isinstance(bboxes, dict):
        with tf.name_scope(scope, 'bboxes_sort_dict'):
            d_scores = {}
            d_bboxes = {}
            for c in scores.keys():
                s, b = bboxes_sort(scores[c], bboxes[c], top_k=top_k)
                d_scores[c] = s
                d_bboxes[c] = b
            return d_scores, d_bboxes

    # Tensors inputs.
    with tf.name_scope(scope, 'bboxes_sort', [scores, bboxes]):
        # Sort scores...
        scores, idxes = tf.nn.top_k(scores, k=top_k, sorted=True)

        # Trick to be able to use tf.gather: map for each element in the first dim.
        def fn_gather(bboxes, idxes):
            bb = tf.gather(bboxes, idxes)
            return [bb]
        r = tf.map_fn(lambda x: fn_gather(x[0], x[1]),
                      [bboxes, idxes],
                      dtype=[bboxes.dtype],
                      parallel_iterations=10,
                      back_prop=False,
                      swap_memory=False,
                      infer_shape=True)
        bboxes = r[0]
        return scores, bboxes

def bboxes_nms_batch(scores, bboxes, nms_threshold=0.5, keep_top_k=200,
                     scope=None):
    """Apply non-maximum selection to bounding boxes. In comparison to TF
    implementation, use classes information for matching.
    Use only on batched-inputs. Use zero-padding in order to batch output
    results.
    Args:
      scores: Batch x N Tensor/Dictionary containing float scores.
      bboxes: Batch x N x 4 Tensor/Dictionary containing boxes coordinates.
      nms_threshold: Matching threshold in NMS algorithm;
      keep_top_k: Number of total object to keep after NMS.
    Return:
      scores, bboxes Tensors/Dictionaries, sorted by score.
        Padded with zero if necessary.
    """
    # Dictionaries as inputs.
    if isinstance(scores, dict) or isinstance(bboxes, dict):
        with tf.name_scope(scope, 'bboxes_nms_batch_dict'):
            d_scores = {}
            d_bboxes = {}
            for c in scores.keys():
                s, b = bboxes_nms_batch(scores[c], bboxes[c],
                                        nms_threshold=nms_threshold,
                                        keep_top_k=keep_top_k)
                d_scores[c] = s
                d_bboxes[c] = b
            return d_scores, d_bboxes

    # Tensors inputs.
    with tf.name_scope(scope, 'bboxes_nms_batch'):
        r = tf.map_fn(lambda x: bboxes_nms(x[0], x[1],
                                           nms_threshold, keep_top_k),
                      (scores, bboxes),
                      dtype=(scores.dtype, bboxes.dtype),
                      parallel_iterations=10,
                      back_prop=False,
                      swap_memory=False,
                      infer_shape=True)
        scores, bboxes = r
        return scores, bboxes

def decode_predictions(overall_predictions, overall_anchors,select_threshold=None, nms_threshold=0.5, clipping_bbox=None, top_k=400, keep_top_k=200)
    """ Decode the boxes given by the network back to the image domain """
    bboxes = []
    for index, (predictions, anchors) in enumerate(zip(overall_predictions, overall_anchors)):
	yref, xref, href, wref = anchors_layer    
	pred_cx = predictions[:, :, :, :, 0] * wref * prior_scaling[0] + xref
	pred_cy = predictions[:, :, :, :, 1] * href * prior_scaling[1] + yref
	pred_w = wref * tf.exp(predictions[:, :, :, :, 2] * prior_scaling[2]
	pred_y = href * tf.exp(predictions[:, :, :, :, 3] * prior_scaling[3]
	
	xmin = pred_cx - pred_w / 2.
	ymin = pred_cy - pred_h / 2.
	xmax = pred_cx + pred_w / 2.
	ymax = pred_cy + pred_h / 2.
        bboxes.append(tf.stack([xmin, ymin, xmax, ymax], axis=-1))

    rscores, rbboxes = tf_ssd_bboxes_select(overall_predictions, bboxes)    

    rscores, rbboxes = bboxes_sort(rscores, rbboxes, top_k=top_k)

    rscores, rbboxes = bboxes_nms_batch(rscores, rbboxes, nms_threshold=nms_threshold, keep_top_k=keep_top_k)

    return rscores, rbboxes

def channel_to_last(inputs,
                    data_format='NHWC',
                    scope=None):
    """Move the channel axis to the last dimension. Allows to
    provide a single output format whatever the input data format.
    Args:
      inputs: Input Tensor;
      data_format: NHWC or NCHW.
    Return:
      Input in NHWC format.
    """
    with tf.name_scope(scope, 'channel_to_last', [inputs]):
        if data_format == 'NHWC':
            net = inputs
        elif data_format == 'NCHW':
            net = tf.transpose(inputs, perm=(0, 2, 3, 1))
        return net

def tensor_shape(x, rank=3):
    """Returns the dimensions of a tensor.
    Args:
      image: A N-D Tensor of shape.
    Returns:
      A list of dimensions. Dimensions that are statically known are python
        integers,otherwise they are integer scalar tensors.
    """
    if x.get_shape().is_fully_defined():
        return x.get_shape().as_list()
    else:
        static_shape = x.get_shape().with_rank(rank).as_list()
        dynamic_shape = tf.unstack(tf.shape(x), rank)
        return [s if s is not None else d
                for s, d in zip(static_shape, dynamic_shape)]
