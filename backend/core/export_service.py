"""
Export Service
===============
Export search results and reports in various formats.
"""

import logging
import io
import json
import csv
from datetime import datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
import base64

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class ExportResult:
    """Result of an export operation"""
    format: str
    filename: str
    content_type: str
    data: bytes
    records_count: int


class ExportService:
    """
    Service for exporting search results and reports.
    
    Supported formats:
    - CSV: Spreadsheet-compatible
    - JSON: Structured data
    - PDF: Formatted report (basic implementation)
    """
    
    def export_search_results(
        self,
        results: Dict,
        format: str = "csv",
        include_images: bool = False
    ) -> ExportResult:
        """
        Export search results to specified format.
        
        Args:
            results: Search results dictionary (from advanced search)
            format: "csv", "json", or "pdf"
            include_images: Include base64 encoded images (json only)
            
        Returns:
            ExportResult with the exported data
        """
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        
        if format == "csv":
            return self._export_csv(results, timestamp)
        elif format == "json":
            return self._export_json(results, timestamp, include_images)
        elif format == "pdf":
            return self._export_pdf(results, timestamp)
        else:
            raise ValueError(f"Unsupported export format: {format}")
    
    def _export_csv(self, results: Dict, timestamp: str) -> ExportResult:
        """Export to CSV format."""
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Header
        writer.writerow([
            'Face Index',
            'Match Rank',
            'Identity ID',
            'Display Name',
            'Type',
            'Similarity',
            'Confidence Band',
            'Last Seen',
            'Appearances Count',
            'Watchlist',
            'Watchlist Alert Level',
            'Quality Score',
            'Quality Warning'
        ])
        
        # Data rows
        records = 0
        for face in results.get('faces', []):
            for rank, match in enumerate(face.get('matches', []), 1):
                watchlist = match.get('watchlist_match', {})
                writer.writerow([
                    face.get('face_index', 0),
                    rank,
                    match.get('identity_id', ''),
                    match.get('display_name', ''),
                    match.get('type', ''),
                    match.get('similarity', 0),
                    match.get('confidence_band', ''),
                    match.get('last_seen_at', ''),
                    match.get('appearances_count', 0),
                    watchlist.get('list_name', ''),
                    watchlist.get('alert_level', ''),
                    face.get('quality_score', 0),
                    face.get('quality_warning', '')
                ])
                records += 1
        
        csv_data = output.getvalue().encode('utf-8')
        
        return ExportResult(
            format="csv",
            filename=f"search_results_{timestamp}.csv",
            content_type="text/csv",
            data=csv_data,
            records_count=records
        )
    
    def _export_json(self, results: Dict, timestamp: str, include_images: bool) -> ExportResult:
        """Export to JSON format."""
        export_data = {
            'export_info': {
                'timestamp': datetime.utcnow().isoformat(),
                'search_id': results.get('search_id'),
                'format_version': '1.0'
            },
            'summary': results.get('summary', {}),
            'image_info': results.get('image_info', {}),
            'faces': [],
            'watchlist_alerts': results.get('watchlist_alerts', [])
        }
        
        records = 0
        for face in results.get('faces', []):
            face_data = {
                'face_index': face.get('face_index'),
                'bounding_box': face.get('bounding_box'),
                'quality_score': face.get('quality_score'),
                'quality_details': face.get('quality_details'),
                'quality_warning': face.get('quality_warning'),
                'skipped': face.get('skipped'),
                'skip_reason': face.get('skip_reason'),
                'matches': face.get('matches', [])
            }
            export_data['faces'].append(face_data)
            records += len(face.get('matches', []))
        
        json_data = json.dumps(export_data, indent=2, default=str).encode('utf-8')
        
        return ExportResult(
            format="json",
            filename=f"search_results_{timestamp}.json",
            content_type="application/json",
            data=json_data,
            records_count=records
        )
    
    def _export_pdf(self, results: Dict, timestamp: str) -> ExportResult:
        """
        Export to PDF format.
        
        Note: This is a basic text-based PDF implementation.
        For production, consider using reportlab or weasyprint.
        """
        # Create a simple text-based PDF
        # This is a minimal implementation - for production, use a proper PDF library
        
        lines = []
        lines.append("FACE SEARCH RESULTS REPORT")
        lines.append("=" * 50)
        lines.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append(f"Search ID: {results.get('search_id', 'N/A')}")
        lines.append("")
        
        # Summary
        summary = results.get('summary', {})
        lines.append("SUMMARY")
        lines.append("-" * 30)
        lines.append(f"Total Faces Detected: {summary.get('total_faces_detected', 0)}")
        lines.append(f"Faces Searchable: {summary.get('faces_searchable', 0)}")
        lines.append(f"Total Matches: {summary.get('total_matches', 0)}")
        lines.append(f"Unique Identities: {summary.get('unique_identities_found', 0)}")
        lines.append(f"Watchlist Alerts: {summary.get('watchlist_alerts', 0)}")
        lines.append("")
        
        # Results
        records = 0
        for face in results.get('faces', []):
            lines.append(f"FACE #{face.get('face_index', 0) + 1}")
            lines.append(f"Quality Score: {face.get('quality_score', 0):.2f}")
            if face.get('quality_warning'):
                lines.append(f"Warning: {face.get('quality_warning')}")
            lines.append("")
            
            for rank, match in enumerate(face.get('matches', []), 1):
                lines.append(f"  Match #{rank}")
                lines.append(f"    Name: {match.get('display_name') or 'Unknown'}")
                lines.append(f"    Type: {match.get('type', 'unknown')}")
                lines.append(f"    Similarity: {match.get('similarity', 0):.2%}")
                lines.append(f"    Confidence: {match.get('confidence_band', 'N/A')}")
                
                watchlist = match.get('watchlist_match')
                if watchlist:
                    lines.append(f"    WATCHLIST: {watchlist.get('list_name')} ({watchlist.get('alert_level')})")
                
                lines.append("")
                records += 1
        
        # Watchlist alerts section
        alerts = results.get('watchlist_alerts', [])
        if alerts:
            lines.append("WATCHLIST ALERTS")
            lines.append("=" * 50)
            for alert in alerts:
                lines.append(f"  - {alert.get('identity_name') or alert.get('identity_id')}")
                lines.append(f"    List: {alert.get('list_name')} ({alert.get('alert_level')})")
                lines.append(f"    Similarity: {alert.get('similarity', 0):.2%}")
                if alert.get('action_instructions'):
                    lines.append(f"    Action: {alert.get('action_instructions')}")
                lines.append("")
        
        # Convert to simple PDF format (minimal implementation)
        text_content = "\n".join(lines)
        
        # For a production system, you would use reportlab here
        # This returns plain text as a fallback
        pdf_data = self._create_minimal_pdf(text_content)
        
        return ExportResult(
            format="pdf",
            filename=f"search_report_{timestamp}.pdf",
            content_type="application/pdf",
            data=pdf_data,
            records_count=records
        )
    
    def _create_minimal_pdf(self, text: str) -> bytes:
        """
        Create a minimal valid PDF document.
        
        Note: This is a very basic PDF. For production use,
        install reportlab and use proper PDF generation.
        """
        # Try to use reportlab if available
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import inch
            
            buffer = io.BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=letter,
                rightMargin=72,
                leftMargin=72,
                topMargin=72,
                bottomMargin=72
            )
            
            styles = getSampleStyleSheet()
            story = []
            
            # Add content
            for line in text.split('\n'):
                if line.startswith('='):
                    continue
                elif line.startswith('FACE SEARCH') or line.startswith('SUMMARY') or line.startswith('WATCHLIST ALERTS'):
                    story.append(Paragraph(line, styles['Heading1']))
                elif line.startswith('FACE #'):
                    story.append(Paragraph(line, styles['Heading2']))
                elif line.strip():
                    story.append(Paragraph(line, styles['Normal']))
                else:
                    story.append(Spacer(1, 12))
            
            doc.build(story)
            return buffer.getvalue()
            
        except ImportError:
            # Fallback: return text file with .pdf extension
            logger.warning("[EXPORT] reportlab not installed, returning text as PDF")
            return text.encode('utf-8')
    
    def export_batch_results(
        self,
        results: Dict,
        format: str = "csv"
    ) -> ExportResult:
        """
        Export batch search results.
        
        Args:
            results: Batch search results dictionary
            format: "csv" or "json"
            
        Returns:
            ExportResult
        """
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        
        if format == "csv":
            return self._export_batch_csv(results, timestamp)
        elif format == "json":
            return self._export_batch_json(results, timestamp)
        else:
            raise ValueError(f"Unsupported export format: {format}")
    
    def _export_batch_csv(self, results: Dict, timestamp: str) -> ExportResult:
        """Export batch results to CSV."""
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Header
        writer.writerow([
            'Image Index',
            'Image Name',
            'Status',
            'Face Index',
            'Match Rank',
            'Identity ID',
            'Display Name',
            'Type',
            'Similarity',
            'Confidence Band',
            'Watchlist',
            'Quality Score'
        ])
        
        records = 0
        for img_result in results.get('results', []):
            if img_result.get('status') == 'error':
                writer.writerow([
                    img_result.get('image_index'),
                    img_result.get('image_name'),
                    'ERROR',
                    '', '', '', '', '', '', '', '', ''
                ])
                records += 1
                continue
            
            if not img_result.get('matches'):
                writer.writerow([
                    img_result.get('image_index'),
                    img_result.get('image_name'),
                    'NO_MATCHES',
                    '', '', '', '', '', '', '', '',
                    img_result.get('quality_scores', [0])[0] if img_result.get('quality_scores') else ''
                ])
                records += 1
                continue
            
            for match in img_result.get('matches', []):
                watchlist = match.get('watchlist_match', {})
                quality_idx = match.get('face_index', 0)
                quality_scores = img_result.get('quality_scores', [])
                quality = quality_scores[quality_idx] if quality_idx < len(quality_scores) else 0
                
                writer.writerow([
                    img_result.get('image_index'),
                    img_result.get('image_name'),
                    'SUCCESS',
                    match.get('face_index', 0),
                    '',  # Rank not tracked per-image in batch
                    match.get('identity_id', ''),
                    match.get('display_name', ''),
                    match.get('type', ''),
                    match.get('similarity', 0),
                    match.get('confidence_band', ''),
                    watchlist.get('list_name', ''),
                    quality
                ])
                records += 1
        
        csv_data = output.getvalue().encode('utf-8')
        
        return ExportResult(
            format="csv",
            filename=f"batch_results_{timestamp}.csv",
            content_type="text/csv",
            data=csv_data,
            records_count=records
        )
    
    def _export_batch_json(self, results: Dict, timestamp: str) -> ExportResult:
        """Export batch results to JSON."""
        export_data = {
            'export_info': {
                'timestamp': datetime.utcnow().isoformat(),
                'batch_id': results.get('batch_id'),
                'format_version': '1.0'
            },
            'summary': {
                'total_images': results.get('total_images'),
                'successful_images': results.get('successful_images'),
                'failed_images': results.get('failed_images'),
                'total_faces_detected': results.get('total_faces_detected'),
                'total_matches': results.get('total_matches'),
                'unique_identities': results.get('unique_identities'),
                'watchlist_alerts': results.get('watchlist_alerts'),
                'processing_time_ms': results.get('processing_time_ms')
            },
            'results': results.get('results', [])
        }
        
        json_data = json.dumps(export_data, indent=2, default=str).encode('utf-8')
        
        return ExportResult(
            format="json",
            filename=f"batch_results_{timestamp}.json",
            content_type="application/json",
            data=json_data,
            records_count=results.get('total_matches', 0)
        )
    
    def export_search_history(
        self,
        history: List[Dict],
        format: str = "csv"
    ) -> ExportResult:
        """
        Export search history.
        
        Args:
            history: List of search history records
            format: "csv" or "json"
            
        Returns:
            ExportResult
        """
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        
        if format == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            
            writer.writerow([
                'Search ID',
                'Type',
                'Date',
                'Scope',
                'Faces Count',
                'Results Count',
                'Unique Identities',
                'Watchlist Alerts',
                'Processing Time (ms)'
            ])
            
            for record in history:
                writer.writerow([
                    record.get('id'),
                    record.get('search_type'),
                    record.get('created_at'),
                    record.get('scope'),
                    record.get('input_faces_count'),
                    record.get('results_count'),
                    record.get('unique_identities_count'),
                    record.get('watchlist_alerts_count'),
                    record.get('processing_time_ms')
                ])
            
            csv_data = output.getvalue().encode('utf-8')
            
            return ExportResult(
                format="csv",
                filename=f"search_history_{timestamp}.csv",
                content_type="text/csv",
                data=csv_data,
                records_count=len(history)
            )
        
        elif format == "json":
            export_data = {
                'export_info': {
                    'timestamp': datetime.utcnow().isoformat(),
                    'format_version': '1.0'
                },
                'history': history
            }
            
            json_data = json.dumps(export_data, indent=2, default=str).encode('utf-8')
            
            return ExportResult(
                format="json",
                filename=f"search_history_{timestamp}.json",
                content_type="application/json",
                data=json_data,
                records_count=len(history)
            )
        
        else:
            raise ValueError(f"Unsupported export format: {format}")


# Global instance
export_service = ExportService()

