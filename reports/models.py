from django.db import models


class ReportCenter(models.Model):
    title = models.CharField(max_length=100, default="Reports")

    class Meta:
        verbose_name = "Reports"
        verbose_name_plural = "Reports"

    def __str__(self):
        return self.title

