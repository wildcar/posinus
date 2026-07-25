import getpass
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or reset the single operator account"

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--password")

    def handle(self, username, password=None, **kwargs):
        password = password or getpass.getpass("Password: ")
        if len(password) < 10:
            raise CommandError("Password must contain at least 10 characters")
        user, _ = get_user_model().objects.get_or_create(username=username, defaults={"is_staff": True, "is_superuser": True})
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()
        self.stdout.write(self.style.SUCCESS(f"Operator {username} is ready"))

