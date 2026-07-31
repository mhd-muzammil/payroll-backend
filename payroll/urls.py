"""
URL configuration for payroll project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path,include
from payroll.health import app_head, health_check, live_check

urlpatterns = [
    path('app.head', app_head, name='app_head'),
    path('livez/', live_check, name='live_check'),
    path('healthz/', health_check, name='health_check'),
    path('admin/', admin.site.urls),
    path("api-auth/", include("rest_framework.urls")),
    path('api/', include('attendance.urls')),
    path('api/', include('employees.urls')),
    path('api/', include('payrollpayslip.urls')),
    path('api/', include('onboarding.urls')),
    path('api/', include('cases.urls')),
    path("api/auth/", include("authentication.urls")),
]

