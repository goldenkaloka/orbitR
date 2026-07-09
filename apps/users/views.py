import uuid
from django.shortcuts import render
from django.views import View
from django.contrib.auth import get_user_model

User = get_user_model()

class DeleteAccountView(View):
    def get(self, request):
        return render(request, 'delete_account.html')

    def post(self, request):
        phone = request.POST.get('phone', '').strip()
        password = request.POST.get('password', '')
        confirm = request.POST.get('confirm')

        if not phone or not password:
            return render(request, 'delete_account.html', {
                'error': 'Phone number and password are required.',
                'phone': phone
            })

        if not confirm:
            return render(request, 'delete_account.html', {
                'error': 'You must confirm that you understand this action is permanent.',
                'phone': phone
            })

        try:
            user = User.objects.get(phone=phone)
        except User.DoesNotExist:
            return render(request, 'delete_account.html', {
                'error': 'Invalid phone number or password. Please verify your credentials.',
                'phone': phone
            })

        if not user.check_password(password):
            return render(request, 'delete_account.html', {
                'error': 'Invalid phone number or password. Please verify your credentials.',
                'phone': phone
            })

        # Process account deletion/anonymization
        try:
            user.username = f"Deleted_{uuid.uuid4().hex[:8]}"
            user.first_name = ""
            user.last_name = ""
            user.email = ""
            user.onesignal_id = None
            user.avatar = None
            user.avatar_url = None
            
            # Anonymize phone and free original number for potential future signup
            user.phone = f"del_{uuid.uuid4().hex[:15]}"
            user.is_active = False
            user.save()
            
            return render(request, 'delete_account.html', {'success': True})
        except Exception as e:
            return render(request, 'delete_account.html', {
                'error': f'An unexpected error occurred while deleting your account: {str(e)}',
                'phone': phone
            })
