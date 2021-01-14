from __future__ import absolute_import

import os.path
import os
import numpy as np
from shapely.geometry import Polygon, asPolygon
from shapely.prepared import prep
from shapely.ops import unary_union
import cv2
from PIL import Image, ImageDraw

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' # i.e. error
from mrcnn import model
from mrcnn.config import Config
import tensorflow as tf
tf.get_logger().setLevel('ERROR')

from ocrd_utils import (
    getLogger,
    make_file_id,
    assert_file_grp_cardinality,
    coordinates_of_segment,
    coordinates_for_segment,
    polygon_from_bbox,
    points_from_polygon,
    MIMETYPE_PAGE
)
from ocrd_models.ocrd_page import (
    to_xml,
    TextRegionType,
    PageType,
    CoordsType
)
from ocrd_models.ocrd_page_generateds import (
    RegionRefType,
    RegionRefIndexedType,
    OrderedGroupType,
    OrderedGroupIndexedType,
    UnorderedGroupType,
    UnorderedGroupIndexedType,
    TextEquivType
)
from ocrd_modelfactory import page_from_file
from ocrd import Processor

from .config import OCRD_TOOL

TOOL = 'ocrd-segment-classify-address-layout'

class AddressConfig(Config):
    """Configuration for detection on address resegmentation"""
    NAME = "address"
    IMAGES_PER_GPU = 1
    GPU_COUNT = 1
    BACKBONE = "resnet50"
    # Number of classes (including background)
    NUM_CLASSES = 3 + 1  # new address model has bg + 3 classes (rcpt/sndr/contact)
    #NUM_CLASSES = 1 + 1  # old address model has bg + 1 classes (rcpt)
    DETECTION_MAX_INSTANCES = 5
    DETECTION_MIN_CONFIDENCE = 0.7
    PRE_NMS_LIMIT = 2000
    POST_NMS_ROIS_INFERENCE = 500
    IMAGE_RESIZE_MODE = "square"
    IMAGE_MIN_DIM = 600
    IMAGE_MAX_DIM = 768
    IMAGE_CHANNEL_COUNT = 5
    MEAN_PIXEL = np.array([123.7, 116.8, 103.9, 0, 0])

class ClassifyAddressLayout(Processor):

    def __init__(self, *args, **kwargs):
        kwargs['ocrd_tool'] = OCRD_TOOL['tools'][TOOL]
        kwargs['version'] = OCRD_TOOL['version']
        super(ClassifyAddressLayout, self).__init__(*args, **kwargs)
        if hasattr(self, 'output_file_grp'):
            # processing context
            self.setup()

    def setup(self):
        LOG = getLogger('processor.ClassifyAddressLayout')
        self.categories = ['',
                           'address-rcpt',
                           'address-sndr',
                           'address-contact']
        def readable(path):
            return os.path.isfile(path) and os.access(path, os.R_OK)
        directories = ['', os.path.dirname(os.path.abspath(__file__))]
        if 'MRCNNDATA' in os.environ:
            directories = [os.environ['MRCNNDATA']] + directories
        model_path = ''
        for directory in directories:
            if readable(os.path.join(directory, self.parameter['model'])):
                model_path = os.path.join(directory, self.parameter['model'])
                break
        if not model_path:
            raise Exception("model file '%s' not found", self.parameter['model'])
        LOG.info("Loading model '%s'", model_path)
        config = AddressConfig()
        config.DETECTION_MIN_CONFIDENCE = self.parameter['min_confidence']
        #config.display()
        self.model = model.MaskRCNN(
            mode="inference", config=config,
            # not really needed, but must be a path...
            model_dir=os.getcwd())
        self.model.load_weights(model_path, by_name=True)

    def process(self):
        """Detect and classify+resegment address regions from text recognition results.
        
        Open and deserialize PAGE input files and their respective images,
        then iterate over the element hierarchy down to the text line level.
        
        Then, get the text classification result for each text line (what
        parts of address descriptions it contains, if any).
        
        Next, retrieve the page image according to the layout annotation (from
        the alternative image of the page, or by cropping at Border and deskewing
        according to @orientation) in raw RGB form. Represent it as an array with
        2 extra channels, one marking text lines, the other marking text lines
        containing address components.
        
        Pass that array to the visual address detector model, and retrieve region
        candidates as tuples of region class, bounding box, and pixel mask.
        Postprocess the mask and bbox to ensure no words are cut off accidentally.
        
        Where the class confidence is high enough, annotate the resulting TextRegion
        (including the special address type), and remove any overlapping input regions
        (re-assigning its text lines).
        
        Produce a new output file by serialising the resulting hierarchy.
        """
        LOG = getLogger('processor.ClassifyAddressLayout')
        assert_file_grp_cardinality(self.input_file_grp, 1)
        assert_file_grp_cardinality(self.output_file_grp, 1)
        
        # pylint: disable=attribute-defined-outside-init
        for n, input_file in enumerate(self.input_files):
            page_id = input_file.pageId or input_file.ID
            LOG.info("INPUT FILE %i / %s", n, page_id)
            pcgts = page_from_file(self.workspace.download_file(input_file))
            self.add_metadata(pcgts)
            
            page = pcgts.get_Page()
            page_image, page_coords, page_image_info = self.workspace.image_from_page(
                page, page_id,
                feature_filter='binarized',
                transparency=False)
            if page_image_info.resolution != 1:
                dpi = page_image_info.resolution
                if page_image_info.resolutionUnit == 'cm':
                    dpi = round(dpi * 2.54)
            else:
                dpi = None
            page_image_binarized, _, _ = self.workspace.image_from_page(
                page, page_id,
                feature_selector='binarized')
            
            page_array_bin = np.array(page_image_binarized)
            if page_array_bin.ndim == 3:
                if page_array_bin.shape[-1] == 3:
                    page_array_bin = np.mean(page_array_bin, 2)
                elif page_array_bin.shape[-1] == 4:
                    dtype = page_array_bin.dtype
                    drange = np.iinfo(dtype).max
                    alpha = np.array(page_array_bin[:,:,3], np.float) / drange
                    color = page_array_bin[:,:,:3]
                    color = color * alpha + drange * (1.0 - alpha)
                    page_array_bin = np.array(np.mean(color, 2), dtype=dtype)
                else:
                    page_array_bin = page_array_bin[:,:,0]
            threshold = 0.5 * (page_array_bin.min() + page_array_bin.max())
            page_array_bin = np.array(page_array_bin <= threshold, np.bool)
            _, components  = cv2.connectedComponents(page_array_bin.astype(np.uint8))
            
            # ensure RGB (if raw was merely grayscale)
            page_image = page_image.convert(mode='RGB')
            # prepare mask image (alpha channel for input image)
            page_image_mask = Image.new(mode='L', size=page_image.size, color=0)
            def mark_line(line):
                text_class = line.get_custom() or ''
                text_class = text_class.replace('subtype: ', '')
                if not text_class.startswith('ADDRESS_'):
                    text_class = 'ADDRESS_NONE'
                # add to mask image (alpha channel for input image)
                polygon = coordinates_of_segment(line, page_image, page_coords)
                # draw line mask:
                ImageDraw.Draw(page_image_mask).polygon(
                    list(map(tuple, polygon.tolist())),
                    fill=200 if text_class == 'ADDRESS_NONE' else 255)
            
            # prepare reading order
            reading_order = dict()
            ro = page.get_ReadingOrder()
            if ro:
                rogroup = ro.get_OrderedGroup() or ro.get_UnorderedGroup()
                if rogroup:
                    page_get_reading_order(reading_order, rogroup)
            
            # iterate through all regions that could have lines
            oldregions = []
            allregions = page.get_AllRegions(classes=['Text'], order='reading-order', depth=2)
            allpolys = [prep(Polygon(coordinates_of_segment(region, page_image, page_coords)))
                        for region in allregions]
            for region in allregions:
                for line in region.get_TextLine():
                    mark_line(line)
            
            # combine raw with aggregated mask to RGBA array
            if page_image.mode.startswith('I') or page_image.mode == 'F':
                # workaround for Pillow#4926
                page_image = page_image.convert('RGB')
            if page_image.mode == '1':
                page_image = page_image.convert('L')
            page_image.putalpha(page_image_mask)
            page_array = np.array(page_image)
            # convert to RGB+Text+Address array
            tmask = page_array[:,:,3:4] > 0
            amask = page_array[:,:,3:4] == 255
            page_array = np.concatenate([page_array[:,:,:3],
                                         255 * tmask.astype(np.uint8),
                                         255 * amask.astype(np.uint8)],
                                        axis=2)
            # predict
            preds = self.model.detect([page_array], verbose=0)[0]
            worse = []
            for i in range(len(preds['class_ids'])):
                for j in range(i + 1, len(preds['class_ids'])):
                    imask = preds['masks'][:,:,i]
                    jmask = preds['masks'][:,:,j]
                    if np.count_nonzero(imask * jmask) / np.count_nonzero(imask + jmask) > 0.5:
                        worse.append(i if preds['scores'][i] < preds['scores'][j] else j)
            best = np.zeros(4)
            for i in range(len(preds['class_ids'])):
                if i in worse:
                    continue
                cat = preds['class_ids'][i]
                score = preds['scores'][i]
                if cat not in [1,2]:
                    # only best probs for sndr and rcpt (other can be many)
                    continue
                if score > best[cat]:
                    best[cat] = score
            if not np.any(best):
                LOG.warning("Detected no sndr/rcpt address on page '%s'", page_id)
            for i in range(len(preds['class_ids'])):
                if i in worse:
                    LOG.debug("Ignoring instance for class %d overlapping better neighbour",
                              preds['class_ids'][i])
                    continue
                cat = preds['class_ids'][i]
                score = preds['scores'][i]
                if not cat:
                    raise Exception('detected region for background class')
                if score < best[cat]:
                    LOG.debug("Degrading instance for class %d with non-maximum score to class 3",
                              preds['class_ids'][i])
                    cat = 3
                name = self.categories[cat]
                mask = preds['masks'][:,:,i]
                bbox = np.around(preds['rois'][i])
                area = np.count_nonzero(mask)
                scale = int(np.sqrt(area)//10)
                scale = scale + (scale+1)%2 # odd
                LOG.debug("post-processing prediction for '%s' at %s area %d scale %d score %f",
                          name, str(bbox), area, scale, score)
                # fill pixel mask from (padded) inner bboxes
                complabels = np.unique(mask * components)
                for label in complabels:
                    if not label:
                        continue # bg/white
                    left, top, w, h = cv2.boundingRect((components==label).astype(np.uint8))
                    right = left + w
                    bottom = top + h
                    left = max(0, left - 4)
                    top = max(0, top - 4)
                    right = min(page_image.width, right + 4)
                    bottom = min(page_image.height, bottom + 4)
                    mask[top:bottom, left:right] = mask.max()
                # dilate and find (outer) contour
                contours = [None, None]
                for _ in range(10):
                    if len(contours) == 1:
                        break
                    mask = cv2.dilate(mask.astype(np.uint8),
                                      np.ones((scale,scale), np.uint8)) > 0
                    contours, _ = cv2.findContours(mask.astype(np.uint8),
                                                   cv2.RETR_EXTERNAL,
                                                   cv2.CHAIN_APPROX_SIMPLE)
                region_polygon = contours[0][:,0,:] # already in x,y order
                #region_polygon = polygon_from_bbox(bbox[1],bbox[0],bbox[3],bbox[2])
                region_polygon = coordinates_for_segment(region_polygon,
                                                         page_image, page_coords)
                region_polygon = polygon_for_parent(region_polygon, page)
                if region_polygon is None:
                    LOG.warning('Ignoring extant region for class %s', category)
                    continue
                # annotate new region
                region_coords = CoordsType(points_from_polygon(region_polygon), conf=score)
                region_id = 'addressregion%02d' % (i+1)
                region = TextRegionType(id=region_id,
                                        Coords=region_coords,
                                        type_='other',
                                        custom='subtype:' + name)
                LOG.info("Detected %s region '%s' on page '%s'",
                         name, region_id, page_id)
                has_address = False
                # remove overlapping existing regions
                region_poly = Polygon(coordinates_of_segment(region, page_image, page_coords))
                for neighbour, neighpoly in zip(allregions, allpolys):
                    if neighbour in oldregions:
                        continue
                    if (neighpoly.within(region_poly) or
                        neighpoly.within(region_poly.buffer(scale)) or
                        (neighpoly.intersects(region_poly) and (
                            neighpoly.context.almost_equals(region_poly) or
                            neighpoly.context.intersection(region_poly).area > 0.8 * neighpoly.context.area))):
                        LOG.debug("removing redundant region '%s' in favour of '%s'",
                                  neighbour.id, region.id)
                        # re-assign text lines
                        line_no = len(region.get_TextLine())
                        for line in neighbour.get_TextLine():
                            if line.get_custom() and line.get_custom().startswith('subtype: ADDRESS_'):
                                has_address = True
                            LOG.debug("stealing text line '%s'", line.id)
                            line.id = region.id + '_line%02d' % line_no
                            line_no += 1
                            region.add_TextLine(line)
                            line_poly = Polygon(coordinates_of_segment(
                                line, page_image, page_coords))
                            if not line_poly.within(region_poly):
                                region_poly = line_poly.union(region_poly)
                                if region_poly.type == 'MultiPolygon':
                                    region_poly = region_poly.convex_hull
                                region_polygon = coordinates_for_segment(
                                    region_poly.exterior.coords[:-1], page_image, page_coords)
                                region.get_Coords().points = points_from_polygon(region_polygon)
                        region.set_TextEquiv([TextEquivType(Unicode='\n'.join(
                            line.get_TextEquiv()[0].Unicode for line in region.get_TextLine()
                            if line.get_TextEquiv()))])
                        # don't re-assign by another address detection
                        oldregions.append(neighbour)
                        # remove old region
                        neighbour.parent_object_.TextRegion.remove(neighbour)
                        if neighbour.id in reading_order:
                            roelem = reading_order[neighbour.id]
                            roelem.set_regionRef(region.id)
                            reading_order[region.id] = roelem
                            del reading_order[neighbour.id]
                    elif neighpoly.crosses(region_poly):
                        LOG.debug("ignoring crossing region '%s' for '%s'",
                                  neighbour.id, region.id)
                    elif neighpoly.overlaps(region_poly):
                        LOG.debug("ignoring overlapping region '%s' for '%s'",
                                  neighbour.id, region.id)
                # safe-guard against ghost detections:
                if has_address:
                    page.add_TextRegion(region)
                else:
                    LOG.info("Ignoring %s region '%s' without any address lines",
                             name, region_id)

            file_id = make_file_id(input_file, self.output_file_grp)
            file_path = os.path.join(self.output_file_grp,
                                     file_id + '.xml')
            pcgts.set_pcGtsId(file_id)
            out = self.workspace.add_file(
                ID=file_id,
                file_grp=self.output_file_grp,
                pageId=input_file.pageId,
                local_filename=file_path,
                mimetype=MIMETYPE_PAGE,
                content=to_xml(pcgts))
            LOG.info('created file ID: %s, file_grp: %s, path: %s',
                     file_id, self.output_file_grp, out.local_filename)

def polygon_for_parent(polygon, parent):
    """Clip polygon to parent polygon range.
    
    (Should be moved to ocrd_utils.coordinates_for_segment.)
    """
    childp = Polygon(polygon)
    if isinstance(parent, PageType):
        if parent.get_Border():
            parentp = Polygon(polygon_from_points(parent.get_Border().get_Coords().points))
        else:
            parentp = Polygon([[0,0], [0,parent.get_imageHeight()],
                               [parent.get_imageWidth(),parent.get_imageHeight()],
                               [parent.get_imageWidth(),0]])
    else:
        parentp = Polygon(polygon_from_points(parent.get_Coords().points))
    # ensure input coords have valid paths (without self-intersection)
    # (this can happen when shapes valid in floating point are rounded)
    childp = make_valid(childp)
    parentp = make_valid(parentp)
    # check if clipping is necessary
    if childp.within(parentp):
        return childp.exterior.coords[:-1]
    # clip to parent
    interp = childp.intersection(parentp)
    # post-process
    if interp.is_empty or interp.area == 0.0:
        return None
    if interp.type == 'GeometryCollection':
        # heterogeneous result: filter zero-area shapes (LineString, Point)
        interp = unary_union([geom for geom in interp.geoms if geom.area > 0])
    if interp.type == 'MultiPolygon':
        # homogeneous result: construct convex hull to connect
        # FIXME: construct concave hull / alpha shape
        interp = interp.convex_hull
    if interp.minimum_clearance < 1.0:
        # follow-up calculations will necessarily be integer;
        # so anticipate rounding here and then ensure validity
        interp = asPolygon(np.round(interp.exterior.coords))
        interp = make_valid(interp)
    return interp.exterior.coords[:-1] # keep open

def make_valid(polygon):
    for split in range(1, len(polygon.exterior.coords)-1):
        if polygon.is_valid or polygon.simplify(polygon.area).is_valid:
            break
        # simplification may not be possible (at all) due to ordering
        # in that case, try another starting point
        polygon = Polygon(polygon.exterior.coords[-split:]+polygon.exterior.coords[:-split])
    for tolerance in range(1, int(polygon.area)):
        if polygon.is_valid:
            break
        # simplification may require a larger tolerance
        polygon = polygon.simplify(tolerance)
    return polygon

def page_get_reading_order(ro, rogroup):
    """Add all elements from the given reading order group to the given dictionary.
    
    Given a dict ``ro`` from layout element IDs to ReadingOrder element objects,
    and an object ``rogroup`` with additional ReadingOrder element objects,
    add all references to the dict, traversing the group recursively.
    """
    regionrefs = list()
    if isinstance(rogroup, (OrderedGroupType, OrderedGroupIndexedType)):
        regionrefs = (rogroup.get_RegionRefIndexed() +
                      rogroup.get_OrderedGroupIndexed() +
                      rogroup.get_UnorderedGroupIndexed())
    if isinstance(rogroup, (UnorderedGroupType, UnorderedGroupIndexedType)):
        regionrefs = (rogroup.get_RegionRef() +
                      rogroup.get_OrderedGroup() +
                      rogroup.get_UnorderedGroup())
    for elem in regionrefs:
        ro[elem.get_regionRef()] = elem
        if not isinstance(elem, (RegionRefType, RegionRefIndexedType)):
            page_get_reading_order(ro, elem)