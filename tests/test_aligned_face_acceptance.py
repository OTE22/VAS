"""Offline camera admission tests; no production database/model is used."""
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import cv2
import numpy as np
from config import settings
from backend.core.aligned_face_quality import assess_aligned_face
from utils.helpers import reference_alignment

IDENTITY = np.array([[1., 0., 0.], [0., 1., 0.]])


def sharp_image():
    y, x = np.indices((112, 112))
    gray = (40 + ((x // 4 + y // 4) % 2) * 170).astype(np.uint8)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        for key, value in {'CAMERA_FACE_ACCEPTANCE_ENABLED': True,
                           'CAMERA_FACE_MIN_SOURCE_COVERAGE': .9,
                           'CAMERA_FACE_MIN_ALIGNED_SHARPNESS': 5.}.items():
            p = patch.object(settings, key, value); p.start(); self.addCleanup(p.stop)

    def assess(self, image, **kwargs):
        return assess_aligned_face(image, kwargs.get('shape', (112,112,3)),
            kwargs.get('inverse', IDENTITY), kwargs.get('landmarks', reference_alignment),
            kwargs.get('bbox', [10,10,102,108]))

    def test_clear_image_passes_and_strong_blur_fails(self):
        self.assertTrue(self.assess(sharp_image())['accepted'])
        blurred = cv2.GaussianBlur(sharp_image(), (31,31), 8)
        result = self.assess(blurred)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['reason'], 'severe_blur')

    def test_missing_source_pixels_rejected_despite_sharp_edges(self):
        # Shift leaves part of the canonical face outside the source; all
        # landmarks themselves remain inside, so the coverage check is needed.
        inverse = np.array([[1.,0.,-25.], [0.,1.,0.]])
        landmarks = reference_alignment - [25,0]
        with patch.object(settings, 'CAMERA_FACE_MIN_SOURCE_COVERAGE', .95):
            result = self.assess(sharp_image(), inverse=inverse, landmarks=landmarks)
        self.assertEqual(result['reason'], 'insufficient_source_coverage')

    def test_natural_dark_pixels_are_not_treated_as_missing_coverage(self):
        image = sharp_image(); image[:, :45] = 0
        result = self.assess(image)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['source_coverage'], 1.)

    def test_padding_boundary_does_not_supply_sharpness(self):
        image = np.full((112,112,3), 90, np.uint8); image[:, :25] = 0
        result = self.assess(image, inverse=np.array([[1.,0.,-25.],[0.,1.,0.]]),
                             landmarks=reference_alignment-[25,0])
        self.assertEqual(result['reason'], 'severe_blur')

    def test_tiny_source_face_cannot_use_legacy_fallback(self):
        self.assertEqual(self.assess(sharp_image(), bbox=[0,0,7,7])['reason'], 'insufficient_source_face')

    def test_landmarks_outside_crop_rejected(self):
        points = reference_alignment.copy(); points[0,0] = -1
        self.assertEqual(self.assess(sharp_image(), landmarks=points)['reason'], 'landmarks_outside_source')

    def test_invalid_transforms_and_input_fail_closed(self):
        for matrix in [np.full((2,3),np.nan),np.zeros((2,3)),np.eye(3)]:
            self.assertFalse(self.assess(sharp_image(), inverse=matrix)['accepted'])
        self.assertFalse(self.assess(None)['accepted'])

    def test_4k_source_uses_same_aligned_sharpness(self):
        inverse = np.array([[10.,0.,900.],[0.,10.,700.]])
        points = reference_alignment * 10 + [900,700]
        result = self.assess(sharp_image(),shape=(2160,3840,3),inverse=inverse,
                             landmarks=points,bbox=[950,800,2000,1800])
        self.assertTrue(result['accepted'])
        self.assertEqual(result['sharpness'],self.assess(sharp_image())['sharpness'])

    def test_explicit_rollback_switch(self):
        with patch.object(settings,'CAMERA_FACE_ACCEPTANCE_ENABLED',False):
            self.assertEqual(self.assess(None)['reason'],'disabled')

    def test_guard_runs_before_embedding_even_with_legacy_scorer(self):
        from backend.services import image_processing as processing
        detector = Mock(); detector.detect.return_value = (np.array([[0,0,112,112,.99]]), np.array([reference_alignment]))
        recognizer = Mock()
        manager = SimpleNamespace(detector=detector,recognizer=recognizer)
        with patch.object(processing,'model_manager',manager), patch.object(settings,'FACE_QUALITY_SCORER','legacy'):
            result = processing._process_crop_sync(np.full((112,112,3),100,np.uint8))
        self.assertFalse(result['acceptance']['accepted'])
        self.assertIsNone(result['embedding'])
        recognizer.get_embedding.assert_not_called()

    def test_accepted_crop_keeps_existing_recognition_path(self):
        from backend.services import image_processing as processing
        detector = Mock(); detector.detect.return_value = (np.array([[0,0,112,112,.99]]), np.array([reference_alignment]))
        recognizer = Mock(); recognizer.get_embedding.return_value=np.ones(512)
        with patch.object(processing,'model_manager',SimpleNamespace(detector=detector,recognizer=recognizer)):
            result = processing._process_crop_sync(sharp_image())
        self.assertEqual(result['stage'],'done')
        self.assertTrue(result['acceptance']['accepted'])
        self.assertAlmostEqual(float(np.linalg.norm(result['embedding'])),1.)
        recognizer.get_embedding.assert_called_once()


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_only_frame_still_persists_without_identity_or_alert(self):
        from backend.services import image_processing as processing
        from backend.core import detection_storage, detection_evidence
        image = np.full((112,112,3),100,np.uint8)
        result = {'error':None, 'stage':'acceptance','kpss':np.array([reference_alignment]),
                  'bboxes':np.array([[0,0,112,112,.99]]),'aligned_face':image,
                  'acceptance':{'accepted':False,'reason':'severe_blur','sharpness':1.}}
        @asynccontextmanager
        async def session(): yield object()
        manager = SimpleNamespace(get_session=session)
        writer = SimpleNamespace(add_detection=AsyncMock())
        with patch.object(processing,'db_manager',manager), \
             patch.object(processing,'ensure_pipeline_registered',AsyncMock()), \
             patch.object(processing,'resolve_display_name',AsyncMock(return_value='Test')), \
             patch.object(processing,'_decode_frame_sync',return_value=image), \
             patch.object(processing,'_process_crop_sync',return_value=result), \
             patch.object(processing,'batch_writer',writer), \
             patch.object(settings,'SAVE_CROPPED_IMAGES',False), \
             patch.object(detection_storage,'save_camera_crop',AsyncMock(return_value='/evidence.jpg')) as save:
            output = await processing.process_image_async(image_bytes=b'frame', predictions=[{'bbox':[0,0,112,112],'class_name':'person','confidence':.9}],
                pipeline_id='quality-test', use_batch_write=True, send_realtime_updates=False)
        self.assertEqual(output['faces'],[])
        self.assertEqual(output['quality_rejected_faces'],1)
        data = writer.add_detection.call_args.args[0]
        evidence = data['faces'][0]
        self.assertIsNone(evidence['identity_id'])
        self.assertIsNone(evidence['_embedding_id'])
        self.assertEqual(evidence['face_image_path'],'/evidence.jpg')
        self.assertIsNone(save.call_args.kwargs['identity_id'])
        # The shared persistence path accepts it as Face evidence, but creates
        # no identity appearance/embedding link/alerts for an unassigned face.
        rows=[]
        async def flush():
            for row in rows:
                if row.__class__.__name__ == 'Detection': row.id=123
        db=SimpleNamespace(add=rows.append,flush=flush,execute=AsyncMock())
        with patch('backend.core.identity_service.identity_service',Mock()) as identities:
            persisted=await detection_evidence.persist_detection(db,detection_data=data)
        self.assertEqual([row.__class__.__name__ for row in rows],['Detection','Face'])
        self.assertEqual(persisted.bundles,[])
        self.assertEqual(persisted.unknown_events,[])
        identities.create_appearance.assert_not_called()
