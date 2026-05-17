from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q

from apps.orders.models import DeliveryBatch, Order
from api.serializers.core import OrderSerializer
from domain.services.batch_dispatch_service import BatchDispatchService


class BatchViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        if user.role == 'driver':
            return DeliveryBatch.objects.filter(driver__user=user)
        elif user.role == 'vendor':
            from apps.restaurants.models import Restaurant
            vendor_restaurants = Restaurant.objects.filter(owner=user).values_list('id', flat=True)
            return DeliveryBatch.objects.filter(batch_restaurants__restaurant_id__in=vendor_restaurants).distinct()
        return DeliveryBatch.objects.none()
    
    def list(self, request):
        queryset = self.get_queryset()
        from api.serializers.core import OrderSerializer
        return Response(OrderSerializer(queryset.first().orders.all() if queryset.exists() else [], many=True).data)
    
    def retrieve(self, request, pk=None):
        try:
            batch = DeliveryBatch.objects.get(id=pk)
            orders = batch.orders.all()
            return Response(OrderSerializer(orders, many=True).data)
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found'}, status=404)
    
    @action(detail=True, methods=['post'], url_path='accept')
    def accept_batch(self, request, pk=None):
        """Driver accepts a batch offer"""
        if request.user.role != 'driver':
            return Response({'error': 'Only drivers can accept batches'}, status=403)
        
        try:
            from apps.delivery.models import Driver
            driver = Driver.objects.get(user=request.user)
            batch = DeliveryBatch.objects.get(id=pk)
            
            result = BatchDispatchService.accept_batch(str(batch.id), str(driver.id), request.user)
            
            orders = batch.orders.all()
            return Response(OrderSerializer(orders, many=True).data)
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found'}, status=404)
        except Driver.DoesNotExist:
            return Response({'error': 'Driver profile not found'}, status=404)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)
        except Exception as e:
            return Response({'error': f'Failed to accept batch: {str(e)}'}, status=400)
    
    @action(detail=True, methods=['post'], url_path='decline')
    def decline_batch(self, request, pk=None):
        """Driver declines a batch offer"""
        if request.user.role != 'driver':
            return Response({'error': 'Only drivers can decline batches'}, status=403)
        
        try:
            from apps.delivery.models import Driver
            driver = Driver.objects.get(user=request.user)
            batch = DeliveryBatch.objects.get(id=pk)
            
            result = BatchDispatchService.decline_batch(str(batch.id), str(driver.id), request.user)
            
            return Response({'status': 'declined', 'batch_id': str(batch.id)})
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found'}, status=404)
        except Driver.DoesNotExist:
            return Response({'error': 'Driver profile not found'}, status=404)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)
        except Exception as e:
            return Response({'error': f'Failed to decline batch: {str(e)}'}, status=400)
    
    @action(detail=False, methods=['get'], url_path='available')
    def available_batches(self, request):
        """Get available batches for driver"""
        if request.user.role != 'driver':
            return Response({'error': 'Only drivers can view available batches'}, status=403)
        
        from apps.delivery.models import Driver
        try:
            driver = Driver.objects.get(user=request.user)
            batches = DeliveryBatch.objects.filter(
                status='CREATED',
                restaurant__h3_index__in=self._get_nearby_h3(driver.h3_index)
            ).prefetch_related('orders', 'orders__restaurant')[:20]
            
            from api.serializers.core import OrderSerializer
            return Response(OrderSerializer([o for b in batches for o in b.orders.all()], many=True).data)
        except Driver.DoesNotExist:
            return Response({'error': 'Driver profile not found'}, status=404)
    
    def _get_nearby_h3(self, h3_index, radius=1):
        if not h3_index:
            return []
        try:
            from apps.core.geo_utils import get_neighbors
            neighbor_cells = get_neighbors(h3_index, radius)
            return neighbor_cells if neighbor_cells else [h3_index]
        except Exception:
            return [h3_index] if h3_index else []