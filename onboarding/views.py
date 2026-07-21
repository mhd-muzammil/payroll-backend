import mimetypes
from pathlib import Path

from django.http import FileResponse, Http404
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from authentication.permissions import IsHRStaff
from .models import Onboarding, Candidate
from .serializers import DOCUMENT_FIELDS, OnboardingSerializer, CandidateSerializer

class OnboardingViewSet(viewsets.ModelViewSet):
    queryset = Onboarding.objects.all().order_by('-created_at')
    serializer_class = OnboardingSerializer
    # Onboarding holds Aadhaar/PAN/bank PII — restrict to HR/admin/superadmin.
    permission_classes = [IsAuthenticated, IsHRStaff]

    @action(
        detail=True,
        methods=["get"],
        url_path=r"documents/(?P<field_name>[a-z_]+)",
        url_name="document",
    )
    def document(self, request, pk=None, field_name=None):
        if field_name not in DOCUMENT_FIELDS:
            raise Http404

        onboarding = self.get_object()
        document = getattr(onboarding, field_name, None)
        if not document or not document.name:
            raise Http404

        try:
            file_handle = document.open("rb")
        except (FileNotFoundError, OSError):
            raise Http404

        filename = Path(document.name).name
        content_type, _ = mimetypes.guess_type(filename)
        response = FileResponse(
            file_handle,
            as_attachment=True,
            filename=filename,
            content_type=content_type or "application/octet-stream",
        )
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response


class CandidateViewSet(viewsets.ModelViewSet):
    queryset = Candidate.objects.all().order_by('-created_at')
    serializer_class = CandidateSerializer
    # Candidate records hold salary slips / bank statements — HR-only.
    permission_classes = [IsAuthenticated, IsHRStaff]
