from django.urls import path

from . import views

urlpatterns = [
    path("hr-mail/", views.MailboxView.as_view(), name="hr-mail"),
    path("hr-mail/sync/", views.SyncView.as_view(), name="hr-mail-sync"),
    path("hr-mail/messages/", views.MessageListView.as_view(), name="hr-mail-messages"),
    path("hr-mail/messages/<int:pk>/", views.MessageView.as_view(), name="hr-mail-message"),
    path("hr-mail/messages/<int:pk>/status/", views.MessageStatusView.as_view(), name="hr-mail-status"),
    path("hr-mail/messages/<int:pk>/reply/", views.ReplyView.as_view(), name="hr-mail-reply"),
    path(
        "hr-mail/messages/<int:pk>/attachments/<int:attachment_id>/",
        views.AttachmentView.as_view(),
        name="hr-mail-attachment",
    ),
    path(
        "hr-mail/messages/<int:pk>/attachments/<int:attachment_id>/ticket/",
        views.AttachmentTicketView.as_view(),
        name="hr-mail-attachment-ticket",
    ),
    # Opened by navigation from the phone app, paid for with the ticket above.
    path("hr-mail/attachment/", views.AttachmentByTicketView.as_view(), name="hr-mail-attachment-file"),
    path("hr-mail/compose/", views.ComposeView.as_view(), name="hr-mail-compose"),
    path("hr-mail/sent/", views.SentView.as_view(), name="hr-mail-sent"),
    path("hr-mail/system/", views.SystemMailView.as_view(), name="hr-mail-system"),
]
